"""Runner session logic: sole client, auth, heartbeat gap, runner-owned grace timer.

Spec §6.1/§6.2: the runner listens; the gateway is the only client. On a heartbeat
gap or read EOF the runner closes the socket and starts a *local* grace timer. A
same-epoch hello before expiry disarms it; expiry (or a new-epoch hello) SIGKILLs
every child and reaps until none remain *before* any inventory can be advertised.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import struct
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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


async def _kill_and_reap_handle(handle: ChildHandle) -> None:
    """SIGKILL a probe child and wait until it exits (best-effort)."""
    with contextlib.suppress(ProcessLookupError, OSError):
        handle.kill()
    wait = getattr(handle, "wait", None)
    if callable(wait):
        with contextlib.suppress(Exception):
            await wait()
        return
    proc = getattr(handle, "proc", None)
    if proc is not None:
        with contextlib.suppress(Exception):
            await proc.wait()


# Gap threshold = 3 heartbeat intervals (10s heartbeat -> 30s gap, spec §6.1).
GAP_FACTOR = 3

# v1 runners (and unconfigured v2 runners) belong to this single placeholder site/region.
DEFAULT_SITE_ID = "local"

# Seconds an unauthenticated network connection may hold the sole-client slot
# (network links only; Unix peers are gated by SO_PEERCRED before this matters).
HELLO_DEADLINE_S = 5.0

# Seconds a single reply write may take; a stalled peer must not wedge the slot holder.
SEND_TIMEOUT_S = 5.0

# Platform-owned skill CLI auth (device_qr / oauth_link). Paths under these roots.
_SKILL_ROOT_PREFIX = Path("/var/lib/portunus/skills").resolve()
_STATE_ROOT_PREFIX = Path("/var/lib/portunus/skill-state").resolve()
# Concrete verbs come from manifest auth.actions; keep names constrained.
_SKILL_AUTH_ACTION_RE = re.compile(r"^[a-z][a-z0-9\-]{0,64}$")
_AUTH_URL_RE = re.compile(r"https://[^\s\"'<>）\]]+")
_SKILL_EXEC_MAX_S = 120.0
_OAUTH_URL_FILE = "oauth-auth-url.txt"
_OAUTH_JOBS: dict[str, "_OauthJob"] = {}


@dataclass
class _OauthJob:
    """Background oauth_link login: browser shim captures URL; CLI keeps polling."""

    job_id: str
    proc: asyncio.subprocess.Process
    state_dir: Path
    auth_url_file: Path
    task: asyncio.Task[tuple[bytes, bytes]]
    auth_url: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    result: Any = None


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

    def abort(self) -> None:
        """Hard-close the transport so the session's blocked reader wakes immediately."""
        with contextlib.suppress(Exception):
            self.writer.transport.abort()

    async def send(self, frame: dict) -> None:
        if self.closed:
            return
        try:
            async with self._send_lock:
                self.writer.write(encode_frame(frame))
                # Bounded: a peer that advertises a tiny window and never reads would
                # otherwise park drain() forever while its handler holds the sole slot.
                async with asyncio.timeout(SEND_TIMEOUT_S):
                    await self.writer.drain()
        except TimeoutError:
            self.abort()  # cannot drain: treat like a broken link so the slot is released
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
        peer_mode: str = "unix",
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
        # unix (SO_PEERCRED uid gate) vs tcp (TLS handshake + in-band token gate).
        self.peer_mode = peer_mode
        self.regions = regions
        self.egress_class = egress_class or None
        self.capacity = capacity
        if peer_mode not in ("unix", "tcp"):
            # This flag selects the peer-credential gate; an unrecognized value must fail
            # closed rather than silently disabling SO_PEERCRED on a Unix listener.
            raise ValueError(f"peer_mode must be 'unix' or 'tcp' (got {peer_mode!r})")
        if peer_mode == "unix" and not dev and expected_uid is None:
            # Network peers authenticate with the TLS handshake + in-band token instead;
            # a remote runner host needs no local gateway user.
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
        if self.peer_mode != "unix":
            # Network link: there is no SO_PEERCRED. Authentication is the TLS handshake
            # (client certs too when PORTUNUS_TLS_CLIENT_CA is set) plus the in-band
            # hello token, exactly as over the Unix socket.
            return True
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
        if self._closing:
            await _close_writer(writer)
            return
        if not self._peer_ok(writer):
            await _close_writer(writer)
            return
        if self._session is not None:
            cur = self._session
            if self.peer_mode == "unix" or cur.authenticated:
                # Sole client (Unix: the uid gate already proved the incumbent's peer).
                await _close_writer(writer)
                return
            # Unauthenticated incumbent (network link): evict it. A legitimate gateway is
            # mid-hello for milliseconds, and _do_hello's sess.closed guards make eviction
            # safe; an attacker holding the slot with junk frames (or a stalled drain) is
            # exactly what must not lock the real gateway out. _peer_ok ran first, so only
            # a credentialed peer (or TLS passer) can evict.
            log.warning("evicting unauthenticated session for a new peer (network link)")
            cur.closed = True
            cur.abort()
        sess = Session(reader, writer, last_heartbeat=asyncio.get_running_loop().time())
        self._session = sess
        try:
            await self._serve(sess)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session error")
        finally:
            sess.closed = True
            if self._session is sess:
                # Probe children never outlive their session — but an evicted (silent)
                # incumbent owns nothing; only the slot owner may touch global task sets.
                await self._cancel_probes()
            await _close_writer(writer)  # fully closed before the slot is released
            if self._session is sess:
                # Arm grace only for the session that still owns the slot: an evicted
                # pre-auth session could otherwise arm the timer under the successor.
                if sess.authenticated:
                    self._arm_grace()
                self._session = None

    async def _serve(self, sess: Session) -> None:
        loop = asyncio.get_running_loop()
        while True:
            remaining = sess.last_heartbeat + self.gap_s - loop.time()
            if self.peer_mode != "unix" and not sess.authenticated:
                # Network link: until a *valid* hello authenticates, the connection may not
                # occupy the sole-client slot for the whole heartbeat gap. frames_read is
                # no guard here — it counts pre-token frames too (slot-starvation DoS).
                remaining = min(remaining, HELLO_DEADLINE_S)
            if remaining <= 0:
                return  # heartbeat gap (or hello deadline on a network link)
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
        elif op == "skill_exec":
            await self._do_skill_exec(sess, msg)
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
        # Safety nets, not reachable today: eviction only targets frame-less sessions, and
        # _do_hello runs after a frame was read. If the eviction rule ever broadens, a
        # supplanted session must still not touch runner state (disarm/reap/epoch).
        if sess.closed:
            return False
        await self._disarm_grace()
        async with self._reap_lock:
            if sess.closed:
                return False
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
        fps: dict[str, str] = {}
        try:
            from pathlib import Path

            from portunus_shared import package_fingerprint

            try:
                fps["portunus_runner"] = package_fingerprint(Path(__file__).resolve().parent)
            except Exception:  # noqa: BLE001 — hello must succeed
                log.warning("runner fingerprint failed", exc_info=True)
            try:
                import portunus_shared

                fps["portunus_shared"] = package_fingerprint(
                    Path(portunus_shared.__file__).resolve().parent
                )
            except Exception:  # noqa: BLE001
                log.warning("shared fingerprint failed", exc_info=True)
            try:
                import portunus_skill_bridge

                fps["portunus_skill_bridge"] = package_fingerprint(
                    Path(portunus_skill_bridge.__file__).resolve().parent
                )
            except Exception:  # noqa: BLE001
                log.warning("skill_bridge fingerprint failed", exc_info=True)
        except Exception:  # noqa: BLE001
            log.warning("fingerprint setup failed", exc_info=True)
        if fps:
            fleet["fingerprints"] = fps
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

    async def _do_skill_exec(self, sess: Session, msg: dict) -> None:
        """One-shot skill CLI under /var/lib/portunus/skills (platform login, not agent MCP)."""
        try:
            result = await _run_skill_exec(msg)
        except PortunusError as e:
            await self._error(sess, msg, e.code, e.message or "skill_exec failed", with_token=True)
            return
        except Exception:
            log.exception("skill_exec failed")
            await self._error(sess, msg, "spawn_failed", "skill_exec failed", with_token=True)
            return
        await self._reply(sess, msg, **result)

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
                await _kill_and_reap_handle(handle)

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
            result, timing = await rec.rpc.call(msg.get("payload"), timeout_s)
            await self._reply(sess, msg, result=result, timing=timing)
        except asyncio.CancelledError:
            raise
        except FrameError as e:  # reply too large for one frame
            await self._error(sess, msg, e.code, "tool result too large", with_token=True)
        except PortunusError as e:
            if e.code == "payload_too_large":
                # stdout is desynchronised mid-line: the child cannot be reused.
                with contextlib.suppress(ProcessLookupError):
                    rec.handle.kill()
            timing = getattr(rec.rpc, "last_timing", None)
            extra: dict[str, Any] = {}
            if isinstance(timing, dict) and timing:
                extra["timing"] = timing
            # Propagate exit evidence so the gateway can free the slot and retry safely.
            if getattr(e, "child_exited", False):
                extra["reason"] = "child_exited"
                with contextlib.suppress(ProcessLookupError):
                    rec.handle.kill()
            await self._error(
                sess, msg, e.code, e.message or "rpc failed", with_token=True, **extra
            )
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


def _under(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise PortunusError("spawn_rejected", f"path escapes {root}")
    return resolved


def _extract_auth_url(parsed: Any) -> str | None:
    if not isinstance(parsed, dict):
        return None
    msg = parsed.get("msg")
    if isinstance(msg, str):
        m = _AUTH_URL_RE.search(msg)
        if m:
            return m.group(0)
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    if isinstance(data, dict):
        for key in ("auth_url", "authorize_url", "url", "revokeUrl"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
    return None


def _parse_cli_json(out: str) -> Any:
    try:
        if out:
            last = out.splitlines()[-1].strip()
            return json.loads(last)
        return None
    except json.JSONDecodeError:
        return {"raw_stdout": out}


def _normalize_openclaw_device_name(skill_root: Path) -> None:
    """OpenClaw skills persist QEMU DMI names under openclaw/config.json — shorten for QR."""
    cfg = skill_root / "openclaw" / "config.json"
    if not cfg.is_file():
        return
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    if data.get("platform") == "Portunus":
        return
    data["platform"] = "Portunus"
    try:
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def _install_browser_shim(state_dir: Path, url_file: Path) -> Path:
    """Fake xdg-open/open (+ short device name): keep page_code polling on headless runners."""
    shim_dir = state_dir / "browser-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    script = shim_dir / "xdg-open"
    script.write_text(
        "#!/bin/sh\n"
        f'URL_FILE="{url_file}"\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        '    http://*|https://*) printf "%s\\n" "$a" > "$URL_FILE"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        'printf "%s\\n" "$1" > "$URL_FILE"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    open_shim = shim_dir / "open"
    if open_shim.exists() or open_shim.is_symlink():
        open_shim.unlink()
    open_shim.symlink_to(script.name)
    # Quark reads `cat /sys/class/dmi/id/product_name` when no persisted platform exists.
    cat_shim = shim_dir / "cat"
    cat_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -eq 1 ] && [ "$1" = "/sys/class/dmi/id/product_name" ]; then\n'
        '  printf "%s\\n" "Portunus"\n'
        "  exit 0\n"
        "fi\n"
        'exec /bin/cat "$@"\n',
        encoding="utf-8",
    )
    cat_shim.chmod(0o755)
    return shim_dir


async def _cancel_oauth_job(job_id: str | None) -> None:
    if not job_id:
        return
    job = _OAUTH_JOBS.pop(job_id, None)
    if job is None:
        return
    if job.proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            job.proc.kill()
        with contextlib.suppress(Exception):
            await job.proc.wait()
    if not job.task.done():
        job.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await job.task


async def _finish_oauth_job(job: _OauthJob) -> None:
    if job.exit_code is not None:
        return
    try:
        out_b, err_b = await job.task
    except Exception:
        out_b, err_b = b"", b""
    job.exit_code = job.proc.returncode
    job.stdout = out_b.decode("utf-8", errors="replace").strip()
    job.stderr = err_b.decode("utf-8", errors="replace").strip()
    job.result = _parse_cli_json(job.stdout)


async def _oauth_job_status(job_id: str) -> dict[str, Any]:
    job = _OAUTH_JOBS.get(job_id)
    if job is None:
        raise PortunusError("spawn_rejected", f"unknown oauth job: {job_id}")
    alive = job.proc.returncode is None
    if not alive:
        await _finish_oauth_job(job)
    auth_url = job.auth_url
    if auth_url is None and job.auth_url_file.is_file():
        raw = job.auth_url_file.read_text(encoding="utf-8", errors="replace").strip()
        if raw.startswith("http"):
            auth_url = raw
            job.auth_url = auth_url
    logged_in = False
    result = job.result
    if isinstance(result, dict) and result.get("code") == 0:
        logged_in = True
    if not alive:
        _OAUTH_JOBS.pop(job_id, None)
    return {
        "exit_code": job.exit_code,
        "result": result,
        "stderr": job.stderr or None,
        "auth_url": auth_url,
        "oauth_job_id": job_id,
        "pending": alive and not logged_in,
        "logged_in": logged_in,
        "qr_png_base64": None,
        "qr_code_path": None,
    }


async def _start_oauth_capture_login(
    *,
    skill_root: Path,
    state_dir: Path,
    script: Path,
    action: str,
    agent_name: str | None,
    agent_profile_dir: str | None,
    extra_env: dict[str, str],
    extra_args: list[str],
    timeout_s: float,
) -> dict[str, Any]:
    """Start login with browser shim; return auth_url as soon as Quark opens it."""
    # One active capture per state dir.
    for jid, existing in list(_OAUTH_JOBS.items()):
        if existing.state_dir == state_dir:
            await _cancel_oauth_job(jid)

    url_file = state_dir / _OAUTH_URL_FILE
    with contextlib.suppress(FileNotFoundError):
        url_file.unlink()
    _normalize_openclaw_device_name(skill_root)
    shim_dir = _install_browser_shim(state_dir, url_file)

    node = shutil.which("node", path="/usr/local/bin:/usr/bin:/bin")
    if not node:
        raise PortunusError("spawn_rejected", "node not found")

    argv = [node, str(script), action]
    if isinstance(agent_name, str) and agent_name.strip():
        argv += ["--agent-name", agent_name.strip()]
    if isinstance(agent_profile_dir, str) and agent_profile_dir.strip():
        profile = _under(_STATE_ROOT_PREFIX, Path(agent_profile_dir))
        profile.mkdir(parents=True, exist_ok=True)
        argv += ["--agent-profile-dir", str(profile)]
    argv.extend(extra_args)

    env = {
        "PATH": f"{shim_dir}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(state_dir),
        "BROWSER": str(shim_dir / "xdg-open"),
        **extra_env,
    }
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(skill_root),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job_id = uuid.uuid4().hex
    task = asyncio.create_task(proc.communicate())
    job = _OauthJob(
        job_id=job_id,
        proc=proc,
        state_dir=state_dir,
        auth_url_file=url_file,
        task=task,
    )
    _OAUTH_JOBS[job_id] = job

    # Wait for shim to capture the authorize URL (Quark calls xdg-open immediately).
    deadline = asyncio.get_running_loop().time() + min(20.0, timeout_s)
    auth_url: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        if url_file.is_file():
            raw = url_file.read_text(encoding="utf-8", errors="replace").strip()
            if raw.startswith("http"):
                auth_url = raw
                break
        if proc.returncode is not None:
            break
        await asyncio.sleep(0.2)

    if auth_url is None:
        # Fall back: process already exited (old cancel path) — parse stdout.
        await _finish_oauth_job(job)
        _OAUTH_JOBS.pop(job_id, None)
        parsed = job.result
        auth_url = _extract_auth_url(parsed)
        return {
            "exit_code": job.exit_code,
            "result": parsed,
            "stderr": job.stderr or None,
            "auth_url": auth_url,
            "oauth_job_id": None,
            "pending": False,
            "logged_in": isinstance(parsed, dict) and parsed.get("code") == 0,
            "qr_png_base64": None,
            "qr_code_path": None,
        }

    job.auth_url = auth_url

    async def _reap() -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        finally:
            await _finish_oauth_job(job)

    asyncio.create_task(_reap())
    return {
        "exit_code": None,
        "result": {
            "code": -106,
            "msg": "等待扫码授权",
            "action": action,
            "type": "result",
            "data": {},
        },
        "stderr": None,
        "auth_url": auth_url,
        "oauth_job_id": job_id,
        "pending": True,
        "logged_in": False,
        "qr_png_base64": None,
        "qr_code_path": None,
    }


async def _run_skill_exec(msg: dict) -> dict[str, Any]:
    """Run ``node <skill_root>/<entrypoint> <action> [identity…] [extra…]``.

    Identity flags and auth env come from the skill manifest — no vendor hardcoding.
    """
    skill_root_raw = msg.get("skill_root")
    state_dir_raw = msg.get("state_dir")
    entrypoint = msg.get("entrypoint")
    action = msg.get("action")
    agent_name = msg.get("agent_name")
    agent_profile_dir = msg.get("agent_profile_dir")
    extra_env = msg.get("env") or {}
    extra_args = msg.get("extra_args") or []
    qr_path_field = msg.get("qr_path_field") or "qr_code_path"
    oauth_capture = msg.get("oauth_capture") is True
    oauth_job_id = msg.get("oauth_job_id")
    oauth_cancel = msg.get("oauth_cancel") is True
    raw_timeout = msg.get("timeout_s")
    timeout_s = _SKILL_EXEC_MAX_S
    if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) and raw_timeout > 0:
        timeout_s = min(float(raw_timeout), _SKILL_EXEC_MAX_S)

    if oauth_cancel and isinstance(oauth_job_id, str):
        await _cancel_oauth_job(oauth_job_id)
        return {
            "exit_code": 0,
            "result": {"code": 0, "msg": "cancelled", "action": "oauth-cancel", "type": "result"},
            "stderr": None,
            "auth_url": None,
            "oauth_job_id": None,
            "pending": False,
            "logged_in": False,
            "qr_png_base64": None,
            "qr_code_path": None,
        }

    if isinstance(oauth_job_id, str) and msg.get("oauth_poll") is True:
        return await _oauth_job_status(oauth_job_id)

    if not isinstance(skill_root_raw, str) or not skill_root_raw:
        raise PortunusError("spawn_rejected", "skill_root required")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise PortunusError("spawn_rejected", "state_dir required")
    if not isinstance(entrypoint, str) or not entrypoint or entrypoint.startswith("/"):
        raise PortunusError("spawn_rejected", "entrypoint must be a relative path")
    if not isinstance(action, str) or not _SKILL_AUTH_ACTION_RE.fullmatch(action):
        raise PortunusError("spawn_rejected", f"action not allowed: {action!r}")
    if not isinstance(extra_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra_env.items()
    ):
        raise PortunusError("spawn_rejected", "env must map strings to strings")
    if agent_name is not None and (not isinstance(agent_name, str) or not agent_name.strip()):
        raise PortunusError("spawn_rejected", "agent_name must be a non-empty string when set")
    if agent_profile_dir is not None and (
        not isinstance(agent_profile_dir, str) or not agent_profile_dir.strip()
    ):
        raise PortunusError("spawn_rejected", "agent_profile_dir must be a non-empty string when set")
    if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
        raise PortunusError("spawn_rejected", "extra_args must be a list of strings")
    if not isinstance(qr_path_field, str) or not qr_path_field.strip() or "/" in qr_path_field:
        raise PortunusError("spawn_rejected", "qr_path_field must be a simple key")

    skill_root = _under(_SKILL_ROOT_PREFIX, Path(skill_root_raw))
    state_dir = _under(_STATE_ROOT_PREFIX, Path(state_dir_raw))
    script = _under(skill_root, skill_root / entrypoint)
    if not script.is_file():
        raise PortunusError("spawn_rejected", f"entrypoint missing: {script}")
    state_dir.mkdir(parents=True, exist_ok=True)

    # Token login supersedes any in-flight capture job for this state dir.
    if extra_args:
        for jid, existing in list(_OAUTH_JOBS.items()):
            if existing.state_dir == state_dir:
                await _cancel_oauth_job(jid)

    if oauth_capture:
        return await _start_oauth_capture_login(
            skill_root=skill_root,
            state_dir=state_dir,
            script=script,
            action=action,
            agent_name=agent_name if isinstance(agent_name, str) else None,
            agent_profile_dir=agent_profile_dir if isinstance(agent_profile_dir, str) else None,
            extra_env=extra_env,
            extra_args=extra_args,
            timeout_s=timeout_s,
        )

    node = shutil.which("node", path="/usr/local/bin:/usr/bin:/bin")
    if not node:
        raise PortunusError("spawn_rejected", "node not found")

    argv = [node, str(script), action]
    if isinstance(agent_name, str) and agent_name.strip():
        argv += ["--agent-name", agent_name.strip()]
    if isinstance(agent_profile_dir, str) and agent_profile_dir.strip():
        profile = _under(_STATE_ROOT_PREFIX, Path(agent_profile_dir))
        profile.mkdir(parents=True, exist_ok=True)
        argv += ["--agent-profile-dir", str(profile)]
    argv.extend(extra_args)

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(state_dir),
        **extra_env,
    }
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(skill_root),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise PortunusError("upstream_timeout", "skill_exec timed out") from None

    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    parsed = _parse_cli_json(out)

    qr_b64 = None
    qr_path = None
    if isinstance(parsed, dict):
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        if isinstance(data, dict):
            raw_path = data.get(qr_path_field.strip())
            if raw_path is None:
                raw_path = data.get("qrImagePath")
            qr_path = raw_path if isinstance(raw_path, str) else None
        if isinstance(qr_path, str) and qr_path:
            for root in (state_dir, skill_root):
                try:
                    qp = _under(root, Path(qr_path))
                    if qp.is_file() and qp.stat().st_size <= 512 * 1024:
                        qr_b64 = base64.b64encode(qp.read_bytes()).decode("ascii")
                        break
                except PortunusError:
                    continue

    return {
        "exit_code": proc.returncode,
        "result": parsed,
        "stderr": err or None,
        "qr_png_base64": qr_b64,
        "qr_code_path": qr_path if isinstance(qr_path, str) else None,
        "auth_url": _extract_auth_url(parsed),
        "pending": False,
        "logged_in": isinstance(parsed, dict) and parsed.get("code") == 0,
    }


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
