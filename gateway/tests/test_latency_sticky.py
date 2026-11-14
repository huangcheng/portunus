"""site_health EWMA updates + skill sticky preferred_site_id (scheduler covered in test_scheduler)."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import pytest
from portunus_gateway.catalog import GUANGYAPAN_MANIFEST, GUANGYAPAN_SKILL_ROOT, GUANGYAPAN_STATE_DIR
from portunus_gateway.db.models import CatalogApp, Server, SiteHealth
from portunus_gateway.db.session import async_session
from portunus_gateway.registry import SiteHealthPolicy, Registry, server_view
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.scheduler import ScheduleDecision, pick_runner, RunnerInfo
from portunus_gateway.vault import Vault

pytestmark = pytest.mark.anyio


def test_sticky_excluded_with_other_sites_refuses():
    # Hard pin must not silently fall over to an empty-state runner.
    assert (
        pick_runner(
            [RunnerInfo(site_id="sticky"), RunnerInfo(site_id="other")],
            preferred_regions=[],
            sticky_site_id="sticky",
            exclude_sites=frozenset({"sticky"}),
        )
        is None
    )


# ---- EWMA writes -------------------------------------------------------------------------


async def _mk_server(db) -> uuid.UUID:
    sid = uuid.uuid4()
    async with async_session() as s:
        s.add(
            Server(
                id=sid,
                name="lat",
                slug=f"lat-{sid.hex[:8]}",
                kind="stdio",
                enabled=True,
                command="/usr/bin/true",
                args=[],
            )
        )
        await s.commit()
    return sid


async def test_ewma_success_only_and_math(db, pepper_file):
    vault = Vault({1: os.urandom(32)})
    reg = Registry(async_session, vault, RunnerPool([]), trusted_path=["/usr/bin"], binary_allowlist=[])
    sid = await _mk_server(db)

    await reg.record_outcome(sid, None, site_id="cn-a", upstream_ms=100)
    lat = await reg.site_latencies(sid)
    assert lat["cn-a"] == pytest.approx(100.0)

    await reg.record_outcome(sid, None, site_id="cn-a", upstream_ms=200)
    lat = await reg.site_latencies(sid)
    alpha = SiteHealthPolicy.ewma_alpha
    expected = alpha * 200.0 + (1.0 - alpha) * 100.0
    assert lat["cn-a"] == pytest.approx(expected)

    # Failures must not poison EWMA.
    await reg.record_outcome(sid, "upstream_timeout", site_id="cn-a", upstream_ms=9999)
    lat2 = await reg.site_latencies(sid)
    assert lat2["cn-a"] == pytest.approx(expected)

    rows = await reg.list_site_health()
    mine = [x for x in rows if x["server_id"] == str(sid) and x["site_id"] == "cn-a"]
    assert mine and mine[0]["sample_count"] == 2 and mine[0]["sticky"] is False


async def test_start_payload_and_server_view_expose_sticky(db, pepper_file, monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: f"/usr/bin/{c}")
    vault = Vault({1: os.urandom(32)})
    reg = Registry(
        async_session,
        vault,
        RunnerPool([]),
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
    sid = uuid.uuid4()
    async with async_session() as s:
        s.add(
            Server(
                id=sid,
                name="lat",
                slug=f"lat-{sid.hex[:8]}",
                kind="stdio",
                enabled=True,
                command="python",
                args=["-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"],
                preferred_site_id="cn-aliyun",
            )
        )
        s.add(
            SiteHealth(
                server_id=sid,
                site_id="cn-aliyun",
                ewma_upstream_ms=55.0,
                sample_count=3,
            )
        )
        await s.commit()
        server = await s.get(Server, sid)
        assert server is not None
        view = server_view(server, [])
    assert view["preferred_site_id"] == "cn-aliyun"
    start = await reg.start_payload(sid)
    assert start["sticky_site_id"] == "cn-aliyun"
    rows = await reg.list_site_health()
    mine = next(x for x in rows if x["server_id"] == str(sid))
    assert mine["sticky"] is True and mine["ewma_upstream_ms"] == pytest.approx(55.0)


async def test_skill_auth_login_sets_preferred_site_logout_clears(db, pepper_file, monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: f"/usr/bin/{c}")
    vault = Vault({1: os.urandom(32)})
    pool = RunnerPool([])
    pool.pick = lambda *a, **k: ScheduleDecision(site_id="cn-aliyun", preferred_miss=False)  # type: ignore[method-assign]
    pool.is_connected = lambda site: site == "cn-aliyun"  # type: ignore[method-assign]
    pool.request = AsyncMock(  # type: ignore[method-assign]
        return_value={"logged_in": True, "result": {"data": {"logged_in": True}}}
    )
    reg = Registry(
        async_session,
        vault,
        pool,
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
    monkeypatch.setattr(reg, "clear_error_and_reprobe", AsyncMock())

    sid = uuid.uuid4()
    slug = f"gyp-{sid.hex[:8]}"
    async with async_session() as s:
        s.add(
            CatalogApp(
                slug=slug,
                label="Guangyapan",
                name="Guangyapan",
                description="test",
                connection="skill_bridge",
                sort_order=0,
                spec={
                    "manifest": GUANGYAPAN_MANIFEST,
                    "env_defaults": {
                        "SKILL_ROOT": GUANGYAPAN_SKILL_ROOT,
                        "STATE_DIR": GUANGYAPAN_STATE_DIR,
                    },
                },
            )
        )
        s.add(
            Server(
                id=sid,
                name="gyp",
                slug=slug,
                kind="stdio",
                enabled=True,
                command="python",
                args=["-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"],
            )
        )
        await s.commit()

    out = await reg.skill_auth(sid, "login", "op")
    assert out.get("logged_in") is True
    async with async_session() as s:
        server = await s.get(Server, sid)
        assert server is not None
        assert server.preferred_site_id == "cn-aliyun"

    start = await reg.start_payload(sid)
    assert start["sticky_site_id"] == "cn-aliyun"

    pool.request = AsyncMock(return_value={"logged_in": False, "result": {}})  # type: ignore[method-assign]
    await reg.skill_auth(sid, "logout", "op")
    async with async_session() as s:
        server = await s.get(Server, sid)
        assert server is not None
        assert server.preferred_site_id is None
    assert (await reg.start_payload(sid))["sticky_site_id"] is None
