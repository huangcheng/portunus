"""Fleet inventory + Logs APIs (audit / tool_calls / retention)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from portunus_gateway.db.models import AuditEvent, ToolCall
from portunus_gateway.db.session import async_session
from portunus_gateway.fleet import build_fleet, db_backend_label
from portunus_gateway.operator_api import create_app, require_operator
from portunus_gateway.registry import Registry
from portunus_gateway.retention import purge_once
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.slots import SlotState
from portunus_gateway.vault import Vault
from sqlalchemy import select

pytestmark = pytest.mark.anyio


class _FakeClient:
    def __init__(
        self,
        site_id: str = "local",
        *,
        connected: bool = False,
        regions: list[str] | None = None,
        fingerprints: dict[str, str] | None = None,
    ) -> None:
        self.site_id = site_id
        self.regions = regions or [site_id]
        self.connected = connected
        self.fingerprints = fingerprints
        self.connected_since = 100.0 if connected else None
        self.last_heartbeat_at = 110.0 if connected else None
        self.last_disconnected_at = None if connected else 90.0
        self.reconnects = 2
        self.egress_class = None
        self.capacity = None


def _vault() -> Vault:
    return Vault({1: os.urandom(32)})


def _registry(pool: RunnerPool | None = None) -> Registry:
    return Registry(
        async_session,
        _vault(),
        pool or RunnerPool(),
        trusted_path=[],
        binary_allowlist=[],
    )


def _app(registry: Registry, *, pool: RunnerPool | None = None, started_at: datetime | None = None):
    app = create_app(registry)
    app.dependency_overrides[require_operator] = lambda: "test-operator"
    if pool is not None:
        app.state.runner_pool = pool
    app.state.started_at = started_at or datetime(2026, 11, 11, 8, 59, 41, tzinfo=timezone.utc)
    return app


async def test_fleet_disconnected_runners_show_no_healthy(db):
    pool = RunnerPool()
    pool.add(_FakeClient("site-a", connected=False), site_id="site-a")
    pool.add(_FakeClient("site-b", connected=False), site_id="site-b")
    registry = _registry(pool)
    app = _app(registry, pool=pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/fleet")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gateway"]["db_backend"] == "sqlite"
    assert body["gateway"]["otel_export"] is False
    assert len(body["runners"]) == 2
    assert all(not row["connected"] for row in body["runners"])
    assert body["children"] == []
    assert body["site_health"] == []


async def test_fleet_reports_fingerprint_drift(db, monkeypatch):
    monkeypatch.setenv("PORTUNUS_TRACE_URL_TEMPLATE", "https://traces.example/{trace_id}")
    gw = {"portunus_shared": "aaa", "portunus_skill_bridge": "bbb"}
    pool = RunnerPool()
    pool.add(
        _FakeClient(
            "site-a",
            connected=True,
            fingerprints={
                "portunus_runner": "rrr",
                "portunus_shared": "aaa",
                "portunus_skill_bridge": "OLD",
            },
        ),
        site_id="site-a",
    )
    monkeypatch.setattr("portunus_gateway.fleet.gateway_fingerprints", lambda: gw)
    body = build_fleet(
        pool=pool,
        slots=None,
        started_at=datetime.now(timezone.utc),
        database_url="postgresql+asyncpg://x/y",
    )
    assert body["gateway"]["db_backend"] == "postgresql"
    assert body["gateway"]["trace_url_template"] == "https://traces.example/{trace_id}"
    assert body["runners"][0]["drift"] == ["portunus_skill_bridge"]


async def test_fleet_children_match_slots(db):
    from types import SimpleNamespace

    from portunus_gateway.slots import Slot

    pool = RunnerPool()
    pool.add(_FakeClient("site-a", connected=True), site_id="site-a")
    sid = uuid.uuid4()
    child_id = "child-1"
    slot = Slot(
        child_id=child_id,
        server_id=sid,
        key_id="key-1",
        state=SlotState.COMMITTED,
        sent_seq=1,
        generation=1,
        vault_version=1,
        site_id="site-a",
        preferred_miss=True,
        pending_stop=False,
    )
    slots = SimpleNamespace(slots={child_id: slot})
    body = build_fleet(
        pool=pool,
        slots=slots,
        started_at=datetime.now(timezone.utc),
        database_url="sqlite:///x",
        server_slug={sid: "quark"},
    )
    assert body["runners"][0]["children"] == 1
    (child,) = body["children"]
    assert child["server"]["slug"] == "quark"
    assert child["preferred_miss"] is True
    assert child["state"] == SlotState.COMMITTED.value


async def test_db_backend_label():
    assert db_backend_label("postgresql+asyncpg://h/db") == "postgresql"
    assert db_backend_label("sqlite+aiosqlite:///x") == "sqlite"


async def test_audit_list_and_cursor(db):
    async with async_session() as s:
        for i in range(3):
            s.add(
                AuditEvent(
                    actor="operator",
                    action="key.mint" if i else "server.enable",
                    target=f"t{i}",
                    detail={"n": i},
                )
            )
        await s.commit()
    app = _app(_registry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/audit", params={"limit": 2})
        assert r.status_code == 200
        page = r.json()
        assert len(page["items"]) == 2
        assert page["next"]
        r2 = await c.get("/api/audit", params={"limit": 2, "before": page["next"]})
        assert r2.status_code == 200
        assert len(r2.json()["items"]) == 1
        filtered = await c.get("/api/audit", params={"action": "key."})
        assert all(i["action"].startswith("key.") for i in filtered.json()["items"])


async def test_tool_calls_list_filters(db):
    sentinel = "SECRET-SHOULD-NOT-APPEAR"
    async with async_session() as s:
        s.add(
            ToolCall(
                trace_id="trace-abc",
                key_id="key-1",
                server_slug="demo",
                tool="echo",
                outcome="ok",
                site_id="site-a",
                gateway_ms=12,
                result_bytes=4,
            )
        )
        s.add(
            ToolCall(
                trace_id="trace-fail",
                key_id="key-1",
                server_slug="demo",
                tool="boom",
                outcome="upstream_error",
                gateway_ms=9,
            )
        )
        await s.commit()
    app = _app(_registry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/tool-calls", params={"outcome": "upstream_error"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["trace_id"] == "trace-fail"
        assert sentinel not in r.text
        by_trace = await c.get("/api/tool-calls", params={"trace_id": "trace-abc"})
        assert by_trace.json()["items"][0]["tool"] == "echo"


async def test_retention_purges_old_rows(db, monkeypatch):
    monkeypatch.setenv("PORTUNUS_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("PORTUNUS_TOOL_CALL_RETENTION_DAYS", "14")
    old_audit = datetime.now(timezone.utc) - timedelta(days=40)
    old_tool = datetime.now(timezone.utc) - timedelta(days=20)
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    async with async_session() as s:
        a_old = AuditEvent(actor="op", action="x", target="t", detail={})
        a_old.created_at = old_audit
        a_new = AuditEvent(actor="op", action="y", target="t", detail={})
        a_new.created_at = fresh
        t_old = ToolCall(
            trace_id="old",
            key_id="k",
            server_slug="s",
            tool="t",
            outcome="ok",
        )
        t_old.created_at = old_tool
        t_new = ToolCall(
            trace_id="new",
            key_id="k",
            server_slug="s",
            tool="t",
            outcome="ok",
        )
        t_new.created_at = fresh
        s.add_all([a_old, a_new, t_old, t_new])
        await s.commit()
    counts = await purge_once()
    assert counts["audit"] == 1
    assert counts["tool_calls"] == 1
    async with async_session() as s:
        assert (await s.execute(select(AuditEvent))).scalars().all()[0].action == "y"
        assert (await s.execute(select(ToolCall))).scalars().all()[0].trace_id == "new"


async def test_fleet_requires_pool(db):
    app = _app(_registry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/fleet")
    assert r.status_code == 503
