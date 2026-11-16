"""Public GET /SKILL.md — domain agent instructions (before SPA StaticFiles)."""

from __future__ import annotations

import os

import httpx
import pytest
from portunus_gateway.app import build_app
from portunus_gateway.db.session import async_session
from portunus_gateway.omni import Omni, ServerStateCache
from portunus_gateway.registry import Registry
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.slots import SlotManager
from portunus_gateway.vault import Vault

pytestmark = pytest.mark.anyio


@pytest.fixture
async def http(db, authz):
    vault = Vault({1: os.urandom(32)})
    pool = RunnerPool([])
    cache = ServerStateCache()
    slots = SlotManager(
        pool,
        authz,
        server_enabled=cache.enabled,
        vault_version=cache.vault_version,
    )
    registry = Registry(
        async_session,
        vault,
        pool,
        trusted_path=["/usr/bin", "/bin"],
        binary_allowlist=[],
        slots=slots,
    )
    omni = Omni(
        session_factory=async_session,
        registry=registry,
        slots=slots,
        runner=pool,
        authz=authz,
        state_cache=cache,
    )
    app = build_app(registry=registry, omni=omni)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


async def test_skill_md_is_public_markdown(http):
    r = await http.get("/SKILL.md")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "portunus_discover_servers" in r.text
    assert "portunus_invoke_tool" in r.text
    assert "User-Agent" in r.text
    assert r.headers.get("cache-control", "").startswith("public")


async def test_skill_md_needs_no_auth(http):
    r = await http.get("/SKILL.md")
    assert r.status_code == 200
    assert "pm-" in r.text or "Bearer" in r.text
