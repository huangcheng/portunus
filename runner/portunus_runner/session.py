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
from collections.abc import Awaitable, Callable
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

from .children import ChildHandle, ChildTable, spawn_dev_simple

log = logging.getLogger("portunus.runner")

SpawnFn = Callable[[dict], Awaitable[ChildHandle]]

# Gap threshold = 3 heartbeat intervals (10s heartbeat -> 30s gap, spec §6.1).
GAP_FACTOR = 3


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


async def _reject_spawn(_msg: dict) -> ChildHandle:
    raise PortunusError("spawn_rejected", "spawn policy not available outside dev mode")


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
    ) -> None:
        if not token:
            raise ValueError("token required")
        if not dev and expected_uid is None:
            raise ValueError("expected_uid required outside dev mode")
        self._token = token
        self._token_b = token.encode()
        self.heartbeat_s = heartbeat_s
        self.grace_s = grace_s
        self.gap_s = heartbeat_s * GAP_FACTOR
        self.dev = dev
        self.expected_uid = expected_uid
        self._spawn: SpawnFn = spawn or (spawn_dev_simple if dev else _reject_spawn)
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
            await self._error(sess, msg, "unauthorized", "invalid token", with_token=False)
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
        else:
            await self._error(sess, msg, "unsupported", f"op {op} not implemented")
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
        await self._reply(sess, msg, run_id=self.run_id, wire_version=wire.WIRE_VERSION)
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
            await self._error(sess, msg, "unsupported", "malformed start")
            return
        if child_id in self.children:
            await self._error(sess, msg, "unsupported", "duplicate child_id", reason="duplicate")
            return
        try:
            handle = await self._spawn(msg)
        except PortunusError as e:
            await self._error(sess, msg, e.code, e.message or "spawn rejected")
            return
        except Exception:
            log.exception("spawn failed")
            await self._error(sess, msg, "spawn_failed", "spawn failed")
            return
        self.children.add(child_id, handle, server_id=server_id)
        await self._reply(sess, msg, child_id=child_id, pid=handle.pid)

    async def _do_signal(self, sess: Session, msg: dict, op: str) -> None:
        child_id = msg.get("child_id")
        rec = self.children.get(child_id) if isinstance(child_id, str) else None
        if rec is None:
            await self._error(
                sess, msg, "unsupported", "unknown child", reason="unknown_child"
            )
            return
        with contextlib.suppress(ProcessLookupError):
            rec.handle.kill() if op == "kill" else rec.handle.terminate()
        await self._reply(sess, msg, child_id=child_id)

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
        with_token: bool = True,
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
        async with self._reap_lock:
            await self.children.kill_all_and_reap()


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
