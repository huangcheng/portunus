"""Long-lived gateway -> runner client (spec §3, §6.1, §6.2).

The runner listens on a Unix socket (local fleet) or a TCP/TLS listener (remote fleet
sites; ``tls://host:port`` addresses — same frame protocol, TLS carriage per the fleet
design) and the gateway is its sole client. This client:

* connects (retrying with capped, jittered backoff until accepted -- the runner rejects a
  connection while it is still closing the previous one), then sends ``hello`` carrying the
  process-wide ``gateway_epoch`` and a fresh per-connection ``conn_id``;
* stamps token + ``gateway_epoch`` + ``conn_id`` on every request;
* heartbeats every ``heartbeat_s`` and treats a missed heartbeat reply as a dead connection;
* correlates replies by ``request_id`` and delivers async ``child_exited`` frames;
* on loss of the connection fails every in-flight request with ``runner_unavailable`` and
  reconnects. ``conn_seq`` increments per established connection so callers can detect that
  work they started belongs to an older connection.

Unix links additionally gate the peer by SO_PEERCRED uid (``expected_uid``); network links
authenticate with the TLS handshake (plus optional client certificate) and the in-band token.

After each successful (re)connect ``on_connected`` runs (the slot manager requests inventory
and reconciles there). If it raises, the connection is dropped and the cycle retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import ipaddress
import logging
import os
import random
import socket
import ssl
import struct
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

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
DEFAULT_SITE_ID = "local"  # v1 runners advertise no fleet metadata: they are the one local site

# Seconds a single frame write may take; a wedged link must hand over to reconnect.
SEND_TIMEOUT_S: float = 5.0


def is_network_address(address: str) -> bool:
    """Whether a runner address names a TCP/TLS listener (``tls://`` / ``tcp://``) vs a Unix path.

    Single source of truth for the transport decision, shared by ``RunnerClient`` and the
    fleet link parser so the uid gate and the transport cannot drift apart.
    """
    return urlparse(address).scheme in ("tls", "tcp")


def _fleet_from_hello(reply: dict) -> tuple[str, list[str], str | None, int | None]:
    """Parse fleet metadata off a hello reply; absent/malformed fields fall back to v1 defaults."""
    site = reply.get("site_id")
    site_id = site.strip() if isinstance(site, str) and site.strip() else DEFAULT_SITE_ID
    raw = reply.get("regions")
    regions = (
        [r.strip() for r in raw if isinstance(r, str) and r.strip()]
        if isinstance(raw, list)
        else []
    ) or [DEFAULT_SITE_ID]
    egress = reply.get("egress_class")
    egress_class = egress if isinstance(egress, str) and egress else None
    cap = reply.get("capacity")
    capacity = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0 else None
    return site_id, regions, egress_class, capacity


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
            # Bounded (runner-side mirror): a black-holed link would otherwise park the
            # heartbeat's drain too, so the missed-heartbeat reconnect never fires.
            # TimeoutError is an OSError: request() aborts and reports runner_unavailable.
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await self.writer.drain()

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()


class _HandshakeError(Exception):
    pass


class RunnerClient:
    def __init__(
        self,
        address: str = DEFAULT_SOCKET,
        token: str = "",
        *,
        epoch: str | None = None,
        heartbeat_s: float = DEFAULTS.heartbeat_s,
        hello_timeout_s: float = 5.0,
        connect_timeout_s: float = 10.0,
        retry_initial_s: float = 0.2,
        retry_max_s: float = 5.0,
        expected_uid: int | None = None,
        dev: bool = False,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        on_disconnected: Callable[[], Awaitable[None] | None] | None = None,
        on_child_exited: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        if not token:
            raise ValueError("token required")
        self.address = address
        # ``tls://host:port`` / ``tcp://host:port`` name a network runner; anything else is a
        # Unix socket path (v1 fleet). is_network_address() owns the transport decision.
        self._dev = dev
        self._net = is_network_address(address)
        parsed = urlparse(address) if self._net else None
        self._host = parsed.hostname if parsed else None
        try:
            self._port = parsed.port if parsed else None
        except ValueError as e:  # non-numeric / out-of-range port
            raise ValueError(f"runner address {address!r} must name a host and port") from e
        self._scheme = parsed.scheme if parsed else None
        if self._net and (not self._host or not self._port):
            # `not self._port` also rejects 0: an ephemeral-port target can never be dialled.
            raise ValueError(f"runner address {address!r} must name a host and port")
        if self._net and expected_uid is not None:
            # Fail fast: the handshake-time check retries forever and looks like "runner down".
            raise ValueError("expected_uid applies to Unix runner links only")
        cert = os.environ.get("PORTUNUS_RUNNER_TLS_CERT", "").strip()
        key = os.environ.get("PORTUNUS_RUNNER_TLS_KEY", "").strip()
        if self._scheme == "tls" and bool(cert) != bool(key):
            # A half-configured client cert fails every handshake against an mTLS runner
            # with only a generic SSLError; treat it as the config error it is.
            raise ValueError(
                "PORTUNUS_RUNNER_TLS_CERT and PORTUNUS_RUNNER_TLS_KEY must be set together"
            )
        # Build (and thereby validate) once: unreadable CA/cert/key files must fail at
        # startup, not surface as a transient-looking connect retry loop.
        try:
            self._ssl_ctx = self._client_ssl_context()
        except OSError as e:
            raise ValueError(f"runner TLS configuration for {address!r}: {e}") from e
        if self._scheme == "tcp" and not self._dev:
            # Numeric loopback literals only: a *name* like localhost resolves at connect
            # time, so a hosts/DNS override would silently send the token off-box in the
            # clear. 127.0.0.2 and the v4-mapped [::ffff:127.0.0.1] stay allowed; the
            # runner-side listener applies the same policy (portunus_runner.main).
            try:
                ip: Any = ipaddress.ip_address(self._host)
            except ValueError:
                ip = None
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            loopback = ip is not None and ip.is_loopback
            if not loopback:
                # Cleartext carries the runner token in the clear; the fleet design allows
                # it for loopback/dev only (see README: Remote runner links).
                raise ValueError(
                    f"runner address {address!r} is cleartext: loopback/dev only (use tls://)"
                )
        self._connect_failures = 0
        self.socket_path = address  # legacy alias: the Unix path, or the URL for logs
        self._token = token
        self._token_b = token.encode()
        # Minted once per gateway process lifetime (spec §3): a restarted gateway is a new epoch.
        self.epoch = epoch or uuid.uuid4().hex
        self.heartbeat_s = heartbeat_s
        self.hello_timeout_s = hello_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.retry_initial_s = retry_initial_s
        self.retry_max_s = retry_max_s
        self.expected_uid = expected_uid
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_child_exited = on_child_exited  # called inline by the reader: must not block

        self.conn_seq = 0
        self.run_id: str | None = None
        # Fleet metadata from the runner's hello reply (v1 compat defaults until/unless advertised).
        self.site_id: str = DEFAULT_SITE_ID
        self.regions: list[str] = [DEFAULT_SITE_ID]
        self.egress_class: str | None = None
        self.capacity: int | None = None
        self.fingerprints: dict[str, str] | None = None
        self.connected_since: float | None = None  # monotonic when hello succeeded
        self.last_heartbeat_at: float | None = None
        self.last_disconnected_at: float | None = None
        self.reconnects: int = 0
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

    def _client_ssl_context(self) -> ssl.SSLContext | None:
        """Client TLS context for ``tls://`` links (env-configured so tests/operators can pin CAs).

        ``PORTUNUS_RUNNER_CA`` trusts a private CA — and *only* that CA: links crossing
        untrusted networks must not remain reachable through any public CA that happens to
        sign the runner's hostname. ``PORTUNUS_RUNNER_TLS_CERT``/``_KEY`` present a client
        certificate to runners requiring mTLS; ``PORTUNUS_RUNNER_TLS_INSECURE=1`` disables
        verification for dev experiments only.
        """
        if self._scheme != "tls":
            return None
        cafile = os.environ.get("PORTUNUS_RUNNER_CA", "").strip() or None
        if cafile:
            # A pinned CA must be the only trust anchor; create_default_context() would
            # load it on top of the system roots and silently weaken the pin.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=cafile)
        else:
            ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        cert = os.environ.get("PORTUNUS_RUNNER_TLS_CERT", "").strip()
        key = os.environ.get("PORTUNUS_RUNNER_TLS_KEY", "").strip()
        if cert and key:  # pair validated in __init__
            ctx.load_cert_chain(cert, key)
        if self._dev and os.environ.get("PORTUNUS_RUNNER_TLS_INSECURE") == "1":
            # Dev escape hatch only: the runner token rides this link, an ungated switch
            # would silently expose it to MITM in production.
            log.warning("runner TLS verification disabled (dev only): %s", self.address)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._net:
            extra: dict[str, Any] = {}
            if self._ssl_ctx is not None:
                # Mirror the runner's bound: a silently-dropped remote link would otherwise
                # stall the graceful SSL shutdown (and with it the reconnect loop) for the
                # 30s default while waiting for a close_notify that never comes.
                extra["ssl_shutdown_timeout"] = 2.0
            return await asyncio.open_connection(
                self._host,
                self._port,
                ssl=self._ssl_ctx,
                server_hostname=self._host if self._scheme == "tls" else None,
                **extra,
            )
        return await asyncio.open_unix_connection(self.socket_path)

    async def _supervise(self) -> None:
        delay = self.retry_initial_s
        while not self._closing:
            try:
                # Bound the attempt: an ACL-dropped remote otherwise hangs in the kernel's
                # connect timeout (minutes), during which this loop can neither retry nor
                # observe _closing. TimeoutError is an OSError, handled like a refused link.
                async with asyncio.timeout(self.connect_timeout_s):
                    reader, writer = await self._open()
                self._connect_failures = 0
            except OSError as e:
                # Warn on the first failure and then occasionally: a TLS misconfiguration is
                # otherwise indistinguishable from a runner that is simply down.
                self._connect_failures += 1
                if self._connect_failures == 1 or self._connect_failures % 10 == 0:
                    log.warning(
                        "runner connect failed (attempt %d): %s: %s",
                        self._connect_failures,
                        self.address,
                        e,
                    )
                # Jittered capped backoff: remote links cross lossy networks; a synchronized
                # retry storm from many gateway processes would only make that worse.
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
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
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))

    def _token_ok(self, msg: dict) -> bool:
        tok = msg.get("token")
        return isinstance(tok, str) and hmac.compare_digest(tok.encode(), self._token_b)

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, conn_id: str
    ) -> dict:
        if self.expected_uid is not None:  # network links are rejected in __init__
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
        self.site_id, self.regions, self.egress_class, self.capacity = _fleet_from_hello(reply)
        fps = reply.get("fingerprints")
        self.fingerprints = (
            {str(k): str(v) for k, v in fps.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(fps, dict)
            else None
        )
        now = asyncio.get_running_loop().time()
        if self.connected_since is not None:
            self.reconnects += 1
        self.connected_since = now
        self.last_heartbeat_at = now
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
            self.last_disconnected_at = asyncio.get_running_loop().time()
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
                self.last_heartbeat_at = asyncio.get_running_loop().time()
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
