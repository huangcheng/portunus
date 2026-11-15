"""Lazy Registry bootstrap shared by Temporal activities (same DB/vault as gateway).

Stdio probes need the gateway's sole-client RunnerPool. Prefer
``bind_gateway_registry()`` from the gateway process; the standalone worker
keeps a RemoteProxy-capable Registry for non-probe activities / tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("portunus.worker.runtime")

_registry: Any = None
_pool_started = False
_gateway_bound = False
_DEFAULT_POLICY = "/etc/portunus/runner-policy.json"


class _StubRunner:
    """Minimal stand-in so Registry can construct when no runner socket is mounted."""

    site_id = "worker-stub"
    connected = False

    def start(self) -> None:
        return None

    async def request(self, *_a, **_k):
        raise RuntimeError("runner not connected in Temporal worker")


def _policy_lists() -> tuple[list[str], list[str]]:
    path = os.environ.get("PORTUNUS_POLICY", _DEFAULT_POLICY)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        trusted, allow = data["trusted_path"], data["binary_allowlist"]
        if not all(isinstance(x, str) for x in [*trusted, *allow]):
            raise ValueError("entries must be strings")
        return list(trusted), list(allow)
    except Exception:  # noqa: BLE001 — worker can still probe remotes without argv policy
        log.warning("worker policy load failed from %s; using empty allowlists", path, exc_info=True)
        return [], []


def bind_gateway_registry(registry: Any) -> None:
    """Use the live gateway Registry (sole-client runner pool already started)."""
    global _registry, _pool_started, _gateway_bound
    _registry = registry
    _pool_started = True
    _gateway_bound = True
    log.warning("worker runtime bound to gateway registry")


def gateway_bound() -> bool:
    return _gateway_bound


def get_registry():
    """Build Registry once. Call ``await ensure_runner_pool()`` from async activities before probing."""
    global _registry
    if _registry is not None:
        return _registry

    from portunus_gateway.config import load_keyring, load_pepper
    from portunus_gateway.db.session import async_session
    from portunus_gateway.main import build_runner_pool, is_dev
    from portunus_gateway.remote import RemoteProxy
    from portunus_gateway.registry import Registry
    from portunus_gateway.runner_pool import RunnerPool
    from portunus_gateway.vault import Vault
    from portunus_shared import DEFAULTS

    load_pepper()
    vault = Vault.from_keyring(load_keyring())
    trusted_path, binary_allowlist = _policy_lists()

    try:
        pool, _client = build_runner_pool(dev=is_dev(), heartbeat_s=DEFAULTS.heartbeat_s)
        log.info("worker runner pool constructed (fleet=%s)", bool(os.environ.get("PORTUNUS_RUNNERS")))
    except Exception:  # noqa: BLE001 — remotes still probe without runners
        log.warning("worker runner pool bootstrap failed; using stub", exc_info=True)
        pool = RunnerPool([_StubRunner()])

    remote = RemoteProxy(async_session, vault)
    registry = Registry(
        async_session,
        vault,
        pool,
        trusted_path=trusted_path,
        binary_allowlist=binary_allowlist,
        remote=remote,
        max_probes=8,
    )
    remote.bind_refresh(registry.refresh_oauth_access_token)
    _registry = registry
    return _registry


async def ensure_runner_pool(*, wait_s: float = 45.0) -> Any:
    """Return a Registry ready for probing.

    When bound to the gateway, the sole-client pool is already running — do not
    dial runners again (second client is rejected). Otherwise start supervise
    tasks on this loop and wait briefly for a link.
    """
    global _pool_started
    reg = get_registry()
    if _gateway_bound:
        return reg
    if not _pool_started:
        try:
            reg.pool.start()
            _pool_started = True
            log.info("worker runner pool start() on running loop")
        except Exception:  # noqa: BLE001
            log.warning("worker runner pool start failed", exc_info=True)
    if reg.any_runner_connected():
        return reg
    deadline = asyncio.get_running_loop().time() + wait_s
    while asyncio.get_running_loop().time() < deadline:
        if reg.any_runner_connected():
            log.info("worker runner connected")
            return reg
        await asyncio.sleep(0.5)
    log.warning(
        "worker still has no connected runner after %.0fs (stdio probes will defer)",
        wait_s,
    )
    return reg
