"""Runner entrypoint: listen on a Unix socket; the gateway is the sole client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pwd
import signal
import stat
import sys

from portunus_shared import DEFAULTS

from .policy import DEFAULT_POLICY_PATH
from .session import DEFAULT_SITE_ID, Runner

DEFAULT_SOCKET = "/run/portunus/runner.sock"


class RunnerServer(Runner):
    def __init__(self, *, socket_path: str, **kwargs) -> None:
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
        srv = RunnerServer(
            socket_path=os.environ.get("PORTUNUS_SOCKET", DEFAULT_SOCKET),
            token=_load_token(),
            heartbeat_s=_env_float("PORTUNUS_HEARTBEAT_S", DEFAULTS.heartbeat_s),
            grace_s=_env_float("PORTUNUS_GRACE_S", DEFAULTS.grace_s),
            dev=dev,
            expected_uid=_expected_uid(),
            policy_path=os.environ.get("PORTUNUS_POLICY", DEFAULT_POLICY_PATH),
            drop_privs=os.environ.get("PORTUNUS_DROP_PRIVS") == "1",
            **fleet_from_env(),
        )
    except ValueError as e:
        raise SystemExit(f"runner config error: {e}") from e
    await srv.start()
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
