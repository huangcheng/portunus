"""Lazy Registry bootstrap shared by Temporal activities (same DB/vault as gateway)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("portunus.worker.runtime")

_registry: Any = None


class _StubRunner:
    """Minimal stand-in so Registry can construct when no runner socket is mounted."""

    site_id = "worker-stub"

    async def request(self, *_a, **_k):
        raise RuntimeError("runner not connected in Temporal worker")


def get_registry():
    global _registry
    if _registry is not None:
        return _registry

    from portunus_gateway.config import load_keyring, load_pepper
    from portunus_gateway.db.session import async_session
    from portunus_gateway.remote import RemoteProxy
    from portunus_gateway.registry import Registry
    from portunus_gateway.runner_pool import RunnerPool
    from portunus_gateway.vault import Vault

    load_pepper()
    vault = Vault.from_keyring(load_keyring())

    runner: Any = _StubRunner()
    sock = os.environ.get("PORTUNUS_SOCKET")
    token_file = os.environ.get("PORTUNUS_TOKEN_FILE")
    if sock and token_file and Path(token_file).is_file():
        try:
            from portunus_gateway.runner_client import RunnerClient

            token = Path(token_file).read_text(encoding="utf-8").strip()
            runner = RunnerClient(sock, token)
            log.info("worker attached to runner socket %s", sock)
        except Exception:  # noqa: BLE001
            log.warning("worker runner attach failed; using stub", exc_info=True)
            runner = _StubRunner()

    pool = RunnerPool([runner])
    remote = RemoteProxy(async_session, vault)
    registry = Registry(async_session, vault, pool, remote=remote)
    remote.bind_refresh(registry.refresh_oauth_access_token)
    _registry = registry
    return _registry
