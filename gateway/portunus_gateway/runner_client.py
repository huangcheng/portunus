"""Long-lived gateway -> runner client (spec §3, §6.1, §6.2).

The runner listens on a Unix socket and the gateway is its sole client. This client:

* connects (retrying with capped backoff until accepted -- the runner rejects a connection
  while it is still closing the previous one), then sends ``hello`` carrying the process-wide
  ``gateway_epoch`` and a fresh per-connection ``conn_id``;
* stamps token + ``gateway_epoch`` + ``conn_id`` on every request;
* heartbeats every ``heartbeat_s`` and treats a missed heartbeat reply as a dead connection;
* correlates replies by ``request_id`` and delivers async ``child_exited`` frames;
* on loss of the connection fails every in-flight request with ``runner_unavailable`` and
  reconnects. ``conn_seq`` increments per established connection so callers can detect that
  work they started belongs to an older connection.

After each successful (re)connect ``on_connected`` runs (the slot manager requests inventory
and reconciles there). If it raises, the connection is dropped and the cycle retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import logging
import socket
import struct
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from portunus_shared import (
    DEFAULTS,
    ERROR_CODES,
    FrameError,
    PortunusError,
    encode_frame,
    read_frame,
)
from portunus_shared.wire import WIRE_VERSION, validate_message

log = logging.getLogger("portunus.gateway.runner")

DEFAULT_SOCKET = "/run/portunus/runner.sock"


class RunnerRequestError(PortunusError):
    """A runner request failed.

    ``sent`` is False only when the frame definitely never reached the socket (so the runner
    cannot have acted on it). ``reason`` is the runner's machine-readable sub-reason
    (e.g. ``unknown_child``) when it supplied one.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        sent: bool = True,
        reason: str | None = None,
        reply: dict | None = None,
    ) -> None:
        super().__init__(code, message)
        self.sent = sent
        self.reason = reason
        self.reply = reply


def _peer_uid(sock: Any) -> int:
    if sys.platform.startswith("linux"):
        _pid, uid, _gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        return uid
    if sys.platform == "darwin":
        data = sock.getsockopt(0, 0x001, 76)  # SOL_LOCAL / LOCAL_PEERCRED -> struct xucred
        return struct.unpack("II", data[:8])[1]
    raise OSError("peer credentials unsupported on this platform")


class _Conn:
    def __init__(self, writer: asyncio.StreamWriter, conn_id: str, seq: int) -> None:
        self.writer = writer
        self.conn_id = conn_id
        self.seq = seq
        self._lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        data = encode_frame(frame)  # FrameError before anything is written
        async with self._lock:
            self.writer.write(data)
            await self.writer.drain()

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()


class _HandshakeError(Exception):
    pass


class RunnerClient:
    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        token: str = "",
        *,
        epoch: str | None = None,
        heartbeat_s: float = DEFAULTS.heartbeat_s,
        hello_timeout_s: float = 5.0,
        retry_initial_s: float = 0.2,
        retry_max_s: float = 5.0,
        expected_uid: int | None = None,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        on_disconnected: Callable[[], Awaitable[None] | None] | None = None,
        on_child_exited: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        if not token:
            raise ValueError("token required")
        self.socket_path = socket_path
        self._token = token
        self._token_b = token.encode()
        # Minted once per gateway process lifetime (spec §3): a restarted gateway is a new epoch.
        self.epoch = epoch or uuid.uuid4().hex
        self.heartbeat_s = heartbeat_s
        self.hello_timeout_s = hello_timeout_s
        self.retry_initial_s = retry_initial_s
        self.retry_max_s = retry_max_s
        self.expected_uid = expected_uid
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_child_exited = on_child_exited  # called inline by the reader: must not block

        self.conn_seq = 0
        self.run_id: str | None = None
        self._conn: _Conn | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._closing = False

    # ---- lifecycle ---------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._supervise())

    async def close(self) -> None:
        self._closing = True
        if self._conn is not None:
            self._conn.abort()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._fail_pending("client closed")

    async def wait_connected(self, timeout: float | None = None) -> None:
        async with asyncio.timeout(timeout):
            while not self.connected:
                await asyncio.sleep(0.01)

    # ---- requests ----------------------------------------------------------------

    async def request(
        self, op: str, *, timeout: float, conn_seq: int | None = None, **fields: Any
    ) -> dict:
        """Send one request and await its reply (fields of the ok-reply are returned).

        ``conn_seq`` pins the request to a connection generation: if the connection changed
        since the caller observed ``self.conn_seq`` the request is refused (``sent=False``).
        """
        conn = self._conn
        if conn is None or (conn_seq is not None and conn_seq != conn.seq):
            raise RunnerRequestError("runner_unavailable", "runner not connected", sent=False)
        request_id = uuid.uuid4().hex
        frame = {
            "version": WIRE_VERSION,
            "op": op,
            "request_id": request_id,
            "token": self._token,
            "gateway_epoch": self.epoch,
            "conn_id": conn.conn_id,
            **fields,
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            try:
                await conn.send(frame)
            except FrameError as e:
                raise RunnerRequestError(e.code, str(e.message), sent=False) from e
            except (ConnectionError, OSError) as e:
                conn.abort()
                raise RunnerRequestError("runner_unavailable", "send failed", sent=True) from e
            try:
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                raise RunnerRequestError(
                    "upstream_timeout", f"runner did not answer {op}", sent=True
                ) from None
        finally:
            self._pending.pop(request_id, None)

    async def inventory(self, *, timeout: float = 10.0, conn_seq: int | None = None) -> dict:
        return await self.request("inventory", timeout=timeout, conn_seq=conn_seq)

    async def stop_child(self, child_id: str, *, timeout: float = 10.0) -> dict:
        return await self.request("stop", timeout=timeout, child_id=child_id)

    async def kill_child(self, child_id: str, *, timeout: float = 10.0) -> dict:
        return await self.request("kill", timeout=timeout, child_id=child_id)

    def _fail_pending(self, message: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RunnerRequestError("runner_unavailable", message, sent=True))
        self._pending.clear()

    # ---- connection loop ---------------------------------------------------------

    async def _supervise(self) -> None:
        delay = self.retry_initial_s
        while not self._closing:
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
            except OSError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max_s)
                continue
            try:
                if await self._run_connection(reader, writer):
                    delay = self.retry_initial_s  # was fully established: restart backoff
                else:
                    delay = min(delay * 2, self.retry_max_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("runner connection error")
            finally:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()
            if not self._closing:
                await asyncio.sleep(delay)

    def _token_ok(self, msg: dict) -> bool:
        tok = msg.get("token")
        return isinstance(tok, str) and hmac.compare_digest(tok.encode(), self._token_b)

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, conn_id: str
    ) -> dict:
        if self.expected_uid is not None:
            try:
                uid = _peer_uid(writer.get_extra_info("socket"))
            except Exception as e:  # fail closed
                raise _HandshakeError("cannot read peer credentials") from e
            if uid != self.expected_uid:
                raise _HandshakeError(f"unexpected runner uid {uid}")
        request_id = uuid.uuid4().hex
        hello = {
            "version": WIRE_VERSION,
            "op": "hello",
            "request_id": request_id,
            "token": self._token,
            "gateway_epoch": self.epoch,
            "conn_id": conn_id,
        }
        try:
            async with asyncio.timeout(self.hello_timeout_s):
                writer.write(encode_frame(hello))
                await writer.drain()
                reply = await read_frame(reader)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, FrameError) as e:
            raise _HandshakeError(f"hello failed: {type(e).__name__}") from e
        try:
            validate_message(reply)
        except FrameError as e:
            raise _HandshakeError("bad hello reply") from e
        if (
            reply.get("op") != "hello"
            or reply.get("request_id") != request_id
            or reply.get("ok") is not True
            or not self._token_ok(reply)
        ):
            raise _HandshakeError("hello rejected")
        return reply

    async def _run_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        conn_id = uuid.uuid4().hex
        try:
            reply = await self._handshake(reader, writer, conn_id)
        except _HandshakeError as e:
            log.info("runner handshake failed: %s", e)
            return False
        self.run_id = reply.get("run_id")
        self.conn_seq += 1
        conn = _Conn(writer, conn_id, self.conn_seq)
        self._conn = conn
        loop = asyncio.get_running_loop()
        hb_task = loop.create_task(self._heartbeat_loop(conn))
        up_task = loop.create_task(self._announce_connected(conn))
        try:
            await self._read_loop(conn, reader)
        finally:
            self._conn = None
            hb_task.cancel()
            up_task.cancel()
            for t in (hb_task, up_task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            self._fail_pending("runner connection lost")
            if self.on_disconnected is not None:
                with contextlib.suppress(Exception):
                    await _maybe_await(self.on_disconnected())
        return True

    async def _announce_connected(self, conn: _Conn) -> None:
        if self.on_connected is None:
            return
        try:
            await self.on_connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_connected failed; dropping connection to retry")
            conn.abort()

    async def _heartbeat_loop(self, conn: _Conn) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await self.request(
                    "heartbeat", timeout=self.heartbeat_s * 2, conn_seq=conn.seq
                )
            except RunnerRequestError:
                conn.abort()  # reader sees EOF and reconnects
                return

    async def _read_loop(self, conn: _Conn, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                msg = await read_frame(reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError, FrameError):
                return
            try:
                validate_message(msg)
            except FrameError:
                return
            if not self._token_ok(msg):
                return  # a peer that cannot present the token is not our runner
            if msg["op"] == "child_exited" and "ok" not in msg:
                if msg.get("gateway_epoch") != self.epoch or msg.get("conn_id") != conn.conn_id:
                    continue  # superseded
                child_id = msg.get("child_id")
                if isinstance(child_id, str) and self.on_child_exited is not None:
                    try:
                        await _maybe_await(self.on_child_exited(child_id))
                    except Exception:
                        log.exception("on_child_exited failed")
                continue
            fut = self._pending.get(msg.get("request_id"))
            if fut is None or fut.done():
                continue
            if msg.get("ok") is True:
                fut.set_result(msg)
            else:
                err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
                code = err.get("code")
                fut.set_exception(
                    RunnerRequestError(
                        code if code in ERROR_CODES else "unsupported",
                        str(err.get("message", "")),
                        reason=msg.get("reason") if isinstance(msg.get("reason"), str) else None,
                        reply=msg,
                    )
                )


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value
