"""Runner entrypoint: listen on a Unix socket; the gateway is the sole client."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import pwd
import signal
import ssl
import stat
import sys
from typing import Any
from urllib.parse import urlparse

from portunus_shared import DEFAULTS

from .policy import DEFAULT_POLICY_PATH
from .session import DEFAULT_SITE_ID, HELLO_DEADLINE_S, Runner

DEFAULT_SOCKET = "/run/portunus/runner.sock"


class RunnerServer(Runner):
    def __init__(self, *, socket_path: str, **kwargs) -> None:
        # Pinned like RunnerNetServer pins "tcp": an overridable mode on a Unix listener
        # would silently skip the SO_PEERCRED gate.
        kwargs["peer_mode"] = "unix"
        super().__init__(**kwargs)
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        _remove_stale_socket(self.socket_path)
        old_umask = os.umask(0o117)  # socket is born 0660: no window of wider access
        try:
            self._server = await asyncio.start_unix_server(
                self.handle_connection, path=self.socket_path
            )
        finally:
            os.umask(old_umask)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        await self.shutdown()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)


def _is_loopback_host(host: str) -> bool:
    """Numeric loopback literals only (127.0.0.0/8, ::1).

    Names like ``localhost`` are excluded: a hosts/DNS override can resolve them off-box,
    at which point a cleartext listener would serve the runner token in the clear. The
    gateway client applies the same policy when dialing ``tcp://`` links.
    """
    try:
        ip: Any = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # [::ffff:127.0.0.1] is loopback too
    return ip.is_loopback


def _parse_listen(raw: str) -> tuple[str, str, int]:
    """``tls://host:port`` / ``tcp://host:port`` → ``(scheme, host, port)``."""
    parsed = urlparse(raw)
    if parsed.scheme not in ("tls", "tcp") or not parsed.hostname or not parsed.port:
        raise ValueError(
            f"PORTUNUS_LISTEN must look like tls://host:port or tcp://host:port (got {raw!r})"
        )
    return parsed.scheme, parsed.hostname, parsed.port


def _server_ssl_context() -> ssl.SSLContext | None:
    """Server TLS context from ``PORTUNUS_TLS_CERT``/``PORTUNUS_TLS_KEY`` (+ optional client CA).

    When ``PORTUNUS_TLS_CLIENT_CA`` is set the runner requires a client certificate (mTLS);
    otherwise the in-band hello token remains the authentication, carried over server-TLS.
    """
    cert = os.environ.get("PORTUNUS_TLS_CERT", "").strip()
    key = os.environ.get("PORTUNUS_TLS_KEY", "").strip()
    if not cert or not key:
        raise ValueError("PORTUNUS_LISTEN=tls:// requires PORTUNUS_TLS_CERT and PORTUNUS_TLS_KEY")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(cert, key)
    except OSError as e:  # unreadable/mismatched cert+key: report like any config error
        raise ValueError(f"cannot load PORTUNUS_TLS_CERT/PORTUNUS_TLS_KEY: {e}") from e
    ca = os.environ.get("PORTUNUS_TLS_CLIENT_CA", "").strip()
    if ca:
        try:
            ctx.load_verify_locations(ca)
        except OSError as e:
            raise ValueError(f"cannot load PORTUNUS_TLS_CLIENT_CA: {e}") from e
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class RunnerNetServer(Runner):
    """Runner listening on TCP: TLS for production links, cleartext loopback/dev only.

    Fleet design: the remote link carries the *same* frame protocol as the Unix socket;
    TLS (with the in-band token, optionally mTLS) is the carriage. Cleartext off-loopback
    is rejected in line with the gateway's https-only egress posture.
    """

    def __init__(self, *, host: str, port: int, ssl_ctx: ssl.SSLContext | None, **kwargs) -> None:
        dev = bool(kwargs.get("dev"))
        if ssl_ctx is None and not dev and not _is_loopback_host(host):
            # Enforced here, not only in amain(): the runner token rides this listener and
            # no second caller (tests, embedders) may create a plaintext off-loopback one.
            raise ValueError(
                "cleartext listeners are loopback/dev only (configure PORTUNUS_TLS_CERT/KEY)"
            )
        # Unconditional: a network listener always uses the tcp gate — an overridable
        # "unix" mode would call peer_cred() on an AF_INET socket and reject everything.
        kwargs["peer_mode"] = "tcp"
        super().__init__(**kwargs)
        self.host = host
        self.port = port
        self._ssl_ctx = ssl_ctx
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        kwargs: dict[str, Any] = {}
        if self._ssl_ctx is not None:
            # The ssl_* timeouts are rejected by create_server when ssl is None. Bound both
            # ends of the handshake: an unfinished one otherwise holds socket + SSL state
            # for the 60s default, and wait_closed() would wait for the peer's close_notify
            # (30s default) after a silent link drop, stalling slot release and shutdown.
            kwargs["ssl_handshake_timeout"] = HELLO_DEADLINE_S
            kwargs["ssl_shutdown_timeout"] = 2.0
        self._server = await asyncio.start_server(
            self.handle_connection,
            host=self.host,
            port=self.port,
            ssl=self._ssl_ctx,
            **kwargs,
        )
        sockets = self._server.sockets or ()
        if sockets:
            if self.port == 0 and len(sockets) > 1:
                # Each port-0 socket gets its own ephemeral port: reporting one would be
                # wrong for the other families. Bind a single address instead.
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                raise RuntimeError(f"{self.host!r} resolved to multiple addresses; bind one")
            self.port = sockets[0].getsockname()[1]  # port 0 → ephemeral port (tests)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        await self.shutdown()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None


def _remove_stale_socket(path: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(f"{path} exists and is not a socket")
    os.unlink(path)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else float(default)


def _load_token() -> str:
    path = os.environ.get("PORTUNUS_TOKEN_FILE")
    if path:
        with open(path) as f:
            token = f.read().strip()
    else:
        token = os.environ.get("PORTUNUS_TOKEN", "").strip()
    if not token:
        raise SystemExit("PORTUNUS_TOKEN_FILE (or PORTUNUS_TOKEN in dev) is required")
    return token


def fleet_from_env() -> dict:
    """Fleet metadata kwargs for ``Runner`` from env (defaults: single ``local`` site)."""
    site_id = os.environ.get("PORTUNUS_SITE_ID", "").strip() or DEFAULT_SITE_ID
    regions = [
        r.strip() for r in os.environ.get("PORTUNUS_REGIONS", "").split(",") if r.strip()
    ] or [DEFAULT_SITE_ID]
    out: dict = {"site_id": site_id, "regions": regions}
    egress = os.environ.get("PORTUNUS_EGRESS_CLASS", "").strip()
    if egress:
        out["egress_class"] = egress
    cap = os.environ.get("PORTUNUS_CAPACITY", "").strip()
    if cap:
        out["capacity"] = int(cap)  # ValueError -> "runner config error"
    return out


def _expected_uid() -> int | None:
    raw = os.environ.get("PORTUNUS_GATEWAY_UID")
    if raw:
        return int(raw)
    try:
        return pwd.getpwnam("portunus").pw_uid
    except KeyError:
        return None


async def amain() -> None:
    dev = os.environ.get("PORTUNUS_DEV") == "1"
    try:
        # Built inside the try: env misconfig (bad heartbeat float, capacity int, uid)
        # must exit as the clean "runner config error" message, not a traceback.
        common = dict(
            token=_load_token(),
            heartbeat_s=_env_float("PORTUNUS_HEARTBEAT_S", DEFAULTS.heartbeat_s),
            grace_s=_env_float("PORTUNUS_GRACE_S", DEFAULTS.grace_s),
            dev=dev,
            expected_uid=_expected_uid(),
            policy_path=os.environ.get("PORTUNUS_POLICY", DEFAULT_POLICY_PATH),
            drop_privs=os.environ.get("PORTUNUS_DROP_PRIVS") == "1",
            **fleet_from_env(),
        )
        listen = os.environ.get("PORTUNUS_LISTEN", "").strip()
        if listen:
            scheme, host, port = _parse_listen(listen)
            if scheme == "tcp" and not dev and not _is_loopback_host(host):
                raise ValueError(
                    "PORTUNUS_LISTEN=tcp:// is cleartext: dev or loopback only "
                    "(use tls:// off-box, per the fleet design)"
                )
            srv = RunnerNetServer(
                host=host,
                port=port,
                ssl_ctx=_server_ssl_context() if scheme == "tls" else None,
                **common,
            )
        else:
            srv = RunnerServer(
                socket_path=os.environ.get("PORTUNUS_SOCKET", DEFAULT_SOCKET),
                **common,
            )
        await srv.start()  # bind failures (EADDRINUSE, multi-address) are config errors too
    except (ValueError, OSError, RuntimeError) as e:
        if isinstance(e, RuntimeError):
            # Not necessarily operator misconfiguration; keep the traceback reachable.
            logging.exception("runner startup failure")
        raise SystemExit(f"runner config error: {e}") from e
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await srv.close()  # kill + reap children before exiting


def cli() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(cli())
