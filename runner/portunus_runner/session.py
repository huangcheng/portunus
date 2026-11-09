"""Runner session logic: sole client, auth, heartbeat gap, runner-owned grace timer.

Spec §6.1/§6.2: the runner listens; the gateway is the only client. On a heartbeat
gap or read EOF the runner closes the socket and starts a *local* grace timer. A
same-epoch hello before expiry disarms it; expiry (or a new-epoch hello) SIGKILLs
every child and reaps until none remain *before* any inventory can be advertised.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import socket
import struct
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from portunus_shared import (
    DEFAULTS,
    FrameError,
    PortunusError,
    encode_frame,
    read_frame,
    wire,
)

from . import probe, rpc
from .children import ChildHandle, ChildTable
from .policy import DEFAULT_POLICY_PATH
from .spawn import Spawner

log = logging.getLogger("portunus.runner")

SpawnFn = Callable[[dict], Awaitable[ChildHandle]]

# Gap threshold = 3 heartbeat intervals (10s heartbeat -> 30s gap, spec §6.1).
GAP_FACTOR = 3

# v1 runners (and unconfigured v2 runners) belong to this single placeholder site/region.
DEFAULT_SITE_ID = "local"


def peer_cred(sock: Any) -> tuple[int | None, int]:
    """Return (pid_or_None, uid) of the peer of a connected AF_UNIX socket."""
    if sys.platform.startswith("linux"):
        data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", data)
        return pid, uid
    if sys.platform == "darwin":
        sol_local, local_peercred, local_peerpid = 0, 0x001, 0x002
        data = sock.getsockopt(sol_local, local_peercred, 76)  # struct xucred
        _ver, uid = struct.unpack("II", data[:8])
        pid = struct.unpack("i", sock.getsockopt(sol_local, local_peerpid, 4))[0]
        return pid, uid
    raise OSError("peer credentials unsupported on this platform")


@dataclass
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    last_heartbeat: float
    authenticated: bool = False
    conn_id: str | None = None
    closed: bool = False
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def send(self, frame: dict) -> None:
        if self.closed:
            return
        try:
            async with self._send_lock:
                self.writer.write(encode_frame(frame))
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass  # read loop observes the broken connection


class Runner:
    def __init__(
        self,
        *,
        token: str,
        heartbeat_s: float = DEFAULTS.heartbeat_s,
        grace_s: float = DEFAULTS.grace_s,
        dev: bool = False,
        expected_uid: int | None = None,
        spawn: SpawnFn | None = None,
        policy_path: str = DEFAULT_POLICY_PATH,
        drop_privs: bool = False,
        stop_kill_s: float = 5.0,
        site_id: str = DEFAULT_SITE_ID,
        regions: Sequence[str] = (DEFAULT_SITE_ID,),
        egress_class: str | None = None,
        capacity: int | None = None,
    ) -> None:
        if not token:
            raise ValueError("token required")
        if not (isinstance(site_id, str) and site_id.strip()):
            raise ValueError("site_id must be a non-empty string")
        regions = [r.strip() for r in regions if isinstance(r, str) and r.strip()]
        if not regions:
            raise ValueError("regions must contain at least one non-empty name")
        if capacity is not None and (
            isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0
        ):
            raise ValueError("capacity must be a non-negative integer")
        # Fleet metadata advertised on the hello reply (soft region affinity; informational).
        self.site_id = site_id.strip()
        self.regions = regions
        self.egress_class = egress_class or None
        self.capacity = capacity
        if not dev and expected_uid is None:
            raise ValueError("expected_uid required outside dev mode")
        self._token = token
        self._token_b = token.encode()
        self.heartbeat_s = heartbeat_s
        self.grace_s = grace_s
        self.gap_s = heartbeat_s * GAP_FACTOR
        self.dev = dev
        self.expected_uid = expected_uid
        self._spawn: SpawnFn = spawn or Spawner(
            policy_path=policy_path, dev=dev, drop_privs=drop_privs
        )
        self.stop_kill_s = stop_kill_s
        self._stop_tasks: set[asyncio.Task] = set()
        self._probes: set[asyncio.Task] = set()  # tools/list probes: not in the child table
        self._rpcs: set[asyncio.Task] = set()  # in-flight rpc bridges (die with the session)
        self.run_id = uuid.uuid4().hex
        self.children = ChildTable(on_exit=self._on_child_exit)

        self._epoch: str | None = None
        self._session: Session | None = None  # sole client slot (set synchronously)
        self._closing = False
        self._grace_task: asyncio.Task | None = None
        self._grace_firing = False
        self._reap_lock = asyncio.Lock()

    # ---- grace timer -------------------------------------------------------------

    @property
    def grace_armed(self) -> bool:
        return self._grace_task is not None and not self._grace_task.done()

    def _arm_grace(self) -> None:
        if self._closing or self.grace_armed:
            return  # never restart a running timer: grace counts from the first disconnect
        self._grace_firing = False
        self._grace_task = asyncio.get_running_loop().create_task(self._grace_main())

    async def _grace_main(self) -> None:
        await asyncio.sleep(self.grace_s)
        self._grace_firing = True  # past the point of no return: hello must wait for the reap
        async with self._reap_lock:
            await self.children.kill_all_and_reap()

    async def _disarm_grace(self) -> None:
        task = self._grace_task
        if task is None or task.done():
            return
        if self._grace_firing:
            # Past the point of no return: let kill+reap finish (never adopt half-killed
            # children). Shield so a cancelled caller cannot abort the reap midway.
            await asyncio.shield(task)
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ---- connection handling -----------------------------------------------------

    def _peer_ok(self, writer: asyncio.StreamWriter) -> bool:
        if self.dev:
            return True  # dev: skip uid check only; token is still required
        try:
            sock = writer.get_extra_info("socket")
            _pid, uid = peer_cred(sock)
        except Exception:
            return False  # fail closed
        return uid == self.expected_uid

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._session is not None or self._closing:
            await _close_writer(writer)  # sole client: reject concurrent connection
            return
        sess = Session(reader, writer, last_heartbeat=asyncio.get_running_loop().time())
        self._session = sess
        try:
            if self._peer_ok(writer):
                await self._serve(sess)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session error")
        finally:
            sess.closed = True
            await self._cancel_probes()  # probe children never outlive their session
            await _close_writer(writer)  # fully closed before the slot is released
            if sess.authenticated:
                self._arm_grace()
            self._session = None

    async def _serve(self, sess: Session) -> None:
        loop = asyncio.get_running_loop()
        while True:
            remaining = sess.last_heartbeat + self.gap_s - loop.time()
            if remaining <= 0:
                return  # heartbeat gap
            try:
                msg = await asyncio.wait_for(read_frame(sess.reader), remaining)
            except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
                return  # gap or EOF
            except FrameError as e:
                await self._error(sess, None, e.code, "bad frame")
                return
            t0 = loop.time()
            keep = await self._dispatch(sess, msg)
            # Pause the gap clock while we were busy handling this frame.
            sess.last_heartbeat += loop.time() - t0
            if not keep:
                return

    # ---- dispatch ----------------------------------------------------------------

    async def _dispatch(self, sess: Session, msg: dict) -> bool:
        try:
            wire.validate_message(msg)
        except FrameError as e:
            await self._error(sess, msg, e.code, str(e.message))
            return False
        token = msg.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self._token_b):
            await self._error(sess, msg, "unauthorized", "invalid token")
            return False
        op = msg["op"]

        if not sess.authenticated:
            if op != "hello":
                await self._error(sess, msg, "unsupported", "hello required")
                return False
            return await self._do_hello(sess, msg)

        if msg.get("gateway_epoch") != self._epoch or msg.get("conn_id") != sess.conn_id:
            return True  # superseded epoch/conn: drop silently

        if op == "heartbeat":
            sess.last_heartbeat = asyncio.get_running_loop().time()
            await self._reply(sess, msg)
        elif op == "inventory":
            async with self._reap_lock:  # never list while a reap is in progress
                await self._reply(sess, msg, run_id=self.run_id, children=self.children.inventory())
        elif op == "start":
            await self._do_start(sess, msg)
        elif op in ("stop", "kill"):
            await self._do_signal(sess, msg, op)
        elif op == "probe":
            await self._do_probe(sess, msg)
        elif op == "rpc":
            self._start_rpc(sess, msg)
        else:
            await self._error(sess, msg, "unsupported", f"op {op} not implemented", with_token=True)
        return True

    async def _do_hello(self, sess: Session, msg: dict) -> bool:
        epoch, conn_id = msg.get("gateway_epoch"), msg.get("conn_id")
        if not (isinstance(epoch, str) and epoch and isinstance(conn_id, str) and conn_id):
            await self._error(sess, msg, "unsupported", "hello needs gateway_epoch and conn_id")
            return False
        await self._disarm_grace()
        async with self._reap_lock:
            if epoch != self._epoch:
                # New epoch (or first ever): kill + reap to empty before replying.
                await self.children.kill_all_and_reap()
                self._epoch = epoch
        sess.conn_id = conn_id
        sess.authenticated = True
        sess.last_heartbeat = asyncio.get_running_loop().time()
        fleet: dict[str, Any] = {"site_id": self.site_id, "regions": list(self.regions)}
        if self.egress_class is not None:
            fleet["egress_class"] = self.egress_class
        if self.capacity is not None:
            fleet["capacity"] = self.capacity
        await self._reply(
            sess, msg, run_id=self.run_id, wire_version=wire.WIRE_VERSION, **fleet
        )
        return True

    async def _do_start(self, sess: Session, msg: dict) -> None:
        child_id, server_id, argv = msg.get("child_id"), msg.get("server_id"), msg.get("argv")
        if not (
            isinstance(child_id, str)
            and child_id
            and isinstance(server_id, str)
            and isinstance(argv, list)
            and argv
            and all(isinstance(a, str) for a in argv)
        ):
            await self._error(sess, msg, "unsupported", "malformed start", with_token=True)
            return
        if child_id in self.children:
            await self._error(sess, msg, "unsupported", "duplicate child_id", reason="duplicate", with_token=True)
            return
        try:
            handle = await self._spawn(msg)
        except PortunusError as e:
            await self._error(sess, msg, e.code, e.message or "spawn rejected", with_token=True)
            return
        except Exception:
            log.exception("spawn failed")
            await self._error(sess, msg, "spawn_failed", "spawn failed", with_token=True)
            return
        self.children.add(child_id, handle, server_id=server_id)
        await self._reply(sess, msg, child_id=child_id, pid=handle.pid)

    async def _do_signal(self, sess: Session, msg: dict, op: str) -> None:
        child_id = msg.get("child_id")
        rec = self.children.get(child_id) if isinstance(child_id, str) else None
        if rec is None:
            await self._error(
                sess, msg, "unsupported", "unknown child", reason="unknown_child", with_token=True
            )
            return
        with contextlib.suppress(ProcessLookupError):
            if op == "kill":
                rec.handle.kill()
            else:
                rec.handle.terminate()
                self._schedule_stop_escalation(rec)
        await self._reply(sess, msg, child_id=child_id)

    def _schedule_stop_escalation(self, rec: Any) -> None:
        """`stop` is SIGTERM; a child that ignores it is SIGKILLed after stop_kill_s."""

        async def _escalate() -> None:
            try:
                await asyncio.wait_for(asyncio.shield(rec.handle.wait()), self.stop_kill_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    rec.handle.kill()
            except Exception:
                pass

        task = asyncio.get_running_loop().create_task(_escalate())
        self._stop_tasks.add(task)
        task.add_done_callback(self._stop_tasks.discard)

    async def _do_probe(self, sess: Session, msg: dict) -> None:
        """Two forms. ``argv`` present: ephemeral ``tools/list`` probe of a fresh child (spec §5).
        Otherwise: liveness probe for a tracked child."""
        if "argv" in msg:
            await self._start_tools_probe(sess, msg)
            return
        child_id = msg.get("child_id")
        rec = self.children.get(child_id) if isinstance(child_id, str) else None
        if rec is None:
            await self._error(
                sess, msg, "unsupported", "unknown child", reason="unknown_child", with_token=True
            )
            return
        alive = rec.handle.returncode is None
        await self._reply(sess, msg, child_id=child_id, pid=rec.handle.pid, alive=alive)

    # ---- tools/list probes -----------------------------------------------------------

    async def _start_tools_probe(self, sess: Session, msg: dict) -> None:
        """Run in a task: a probe can take up to ``probe_timeout_s`` and must not stall the
        read loop (heartbeats). Capped at ``max_probes`` outside the child table and caps."""
        if len(self._probes) >= DEFAULTS.max_probes:
            await self._error(sess, msg, "child_cap", "probe pool busy", with_token=True)
            return
        task = asyncio.get_running_loop().create_task(self._run_tools_probe(sess, msg))
        self._probes.add(task)
        task.add_done_callback(self._probes.discard)

    async def _run_tools_probe(self, sess: Session, msg: dict) -> None:
        raw = msg.get("timeout_s")
        timeout_s = float(DEFAULTS.probe_timeout_s)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            timeout_s = min(float(raw), timeout_s)  # the gateway can shorten, never extend
        handle: ChildHandle | None = None
        try:
            handle = await self._spawn(msg)  # same argv grammar + policy as `start`
            proc = getattr(handle, "proc", None)
            if proc is None:
                raise PortunusError("spawn_failed", "spawner does not expose stdio")
            tools = await probe.list_tools(proc, timeout_s)
            await self._reply(sess, msg, tools=tools)
        except asyncio.CancelledError:
            raise
        except FrameError as e:
            await self._error(sess, msg, e.code, "probe result too large", with_token=True)
        except PortunusError as e:
            await self._error(sess, msg, e.code, e.message or "probe failed", with_token=True)
        except Exception:
            log.exception("probe failed")
            await self._error(sess, msg, "spawn_failed", "probe failed", with_token=True)
        finally:
            if handle is not None:
                await _kill_and_reap(handle)

    # ---- rpc ---------------------------------------------------------------------

    def _start_rpc(self, sess: Session, msg: dict) -> None:
        """Run in a task: a tool call can take up to its timeout and must not stall the read
        loop (heartbeats)."""
        task = asyncio.get_running_loop().create_task(self._do_rpc(sess, msg))
        self._rpcs.add(task)
        task.add_done_callback(self._rpcs.discard)

    async def _do_rpc(self, sess: Session, msg: dict) -> None:
        child_id = msg.get("child_id")
        rec = self.children.get(child_id) if isinstance(child_id, str) else None
        if rec is None:
            await self._error(
                sess, msg, "unsupported", "unknown child", reason="unknown_child", with_token=True
            )
            return
        proc = getattr(rec.handle, "proc", None)
        if proc is None:
            await self._error(sess, msg, "spawn_failed", "child has no stdio", with_token=True)
            return
        if rec.rpc is None:
            rec.rpc = rpc.ChildRpc(proc)
        timeout_s = rpc.clamp_timeout(msg.get("timeout_s"), float(DEFAULTS.invoke_timeout_s))
        try:
            result = await rec.rpc.call(msg.get("payload"), timeout_s)
            await self._reply(sess, msg, result=result)
        except asyncio.CancelledError:
            raise
        except FrameError as e:  # reply too large for one frame
            await self._error(sess, msg, e.code, "tool result too large", with_token=True)
        except PortunusError as e:
            if e.code == "payload_too_large":
                # stdout is desynchronised mid-line: the child cannot be reused.
                with contextlib.suppress(ProcessLookupError):
                    rec.handle.kill()
            await self._error(sess, msg, e.code, e.message or "rpc failed", with_token=True)
        except Exception:
            log.exception("rpc failed")
            await self._error(sess, msg, "upstream_crash", "rpc failed", with_token=True)

    async def _cancel_probes(self) -> None:
        tasks = list(self._probes) + list(self._rpcs)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- replies -----------------------------------------------------------------

    async def _reply(self, sess: Session, msg: dict, **fields: Any) -> None:
        await sess.send(
            {
                "version": wire.WIRE_VERSION,
                "op": msg["op"],
                "request_id": msg.get("request_id"),
                "token": self._token,
                "ok": True,
                **fields,
            }
        )

    async def _error(
        self,
        sess: Session,
        msg: dict | None,
        code: str,
        message: str = "",
        *,
        with_token: bool = False,
        **fields: Any,
    ) -> None:
        frame: dict[str, Any] = {
            "version": wire.WIRE_VERSION,
            "op": (msg or {}).get("op") if isinstance((msg or {}).get("op"), str) else "error",
            "request_id": (msg or {}).get("request_id"),
            "ok": False,
            "error": {"code": code, "message": message},
            **fields,
        }
        if with_token:
            frame["token"] = self._token
        await sess.send(frame)

    async def _on_child_exit(self, child_id: str, code: int | None) -> None:
        sess = self._session
        if sess is None or not sess.authenticated or sess.closed:
            return
        await sess.send(
            wire.child_exited(
                request_id=uuid.uuid4().hex,
                token=self._token,
                child_id=child_id,
                exit_code=code,
                gateway_epoch=self._epoch,
                conn_id=sess.conn_id,
            )
        )

    # ---- shutdown ----------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop serving: drop client, cancel grace, kill + reap everything."""
        self._closing = True
        sess = self._session
        if sess is not None:
            await _close_writer(sess.writer)
        task = self._grace_task
        if task is not None and not task.done() and not self._grace_firing:
            task.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for t in list(self._stop_tasks):
            t.cancel()
        await self._cancel_probes()
        async with self._reap_lock:
            await self.children.kill_all_and_reap()


async def _kill_and_reap(handle: ChildHandle) -> None:
    """SIGKILL and wait; shielded so a cancelled probe still reaps its child."""
    with contextlib.suppress(ProcessLookupError):
        handle.kill()
    waiter = asyncio.ensure_future(handle.wait())
    try:
        await asyncio.shield(waiter)
    except asyncio.CancelledError:
        await asyncio.gather(waiter, return_exceptions=True)
        raise
    except Exception:
        pass


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
