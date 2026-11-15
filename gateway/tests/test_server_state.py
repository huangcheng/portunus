"""Server health derivation + observation write-back (spec 2026-11-11-server-state)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from portunus_gateway.db.models import CatalogApp, Server, Tool
from portunus_gateway.db.session import async_session
from portunus_gateway.health import (
    HealthFacts,
    derive_status,
    health_fields,
    should_record_failure,
    should_throttle_success,
)
from portunus_gateway.registry import Registry, RegistryError
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.vault import Vault


def _vault() -> Vault:
    return Vault({1: os.urandom(32)})


def _facts(**overrides) -> HealthFacts:
    base = dict(
        kind="stdio",
        oauth=None,
        has_authorization=False,
        requires_skill_session=False,
        has_skill_session=False,
        probing=False,
        runner_connected=True,
        last_probe_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        last_ok_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        last_error_code=None,
        last_error_at=None,
        enabled_tool_count=3,
    )
    base.update(overrides)
    return HealthFacts(**base)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"probing": True}, "probing"),
        (
            {
                "last_probe_at": None,
                "last_error_at": None,
                "last_ok_at": None,
                "enabled_tool_count": 0,
            },
            "probing",
        ),
        # Live traffic alone is enough to leave probing (review #23).
        (
            {
                "last_probe_at": None,
                "last_error_at": None,
                "last_ok_at": datetime(2026, 11, 11, tzinfo=timezone.utc),
                "enabled_tool_count": 2,
            },
            "ok",
        ),
        (
            {
                "kind": "remote",
                "oauth": {"authorize_url": "https://x", "token_url": "https://y", "client_id": "c"},
                "has_authorization": False,
            },
            "auth_required",
        ),
        (
            {
                "requires_skill_session": True,
                "has_skill_session": False,
            },
            "auth_required",
        ),
        ({"kind": "stdio", "runner_connected": False}, "unreachable"),
        (
            {
                "last_error_code": "auth_required",
                "last_error_at": datetime(2026, 11, 12, tzinfo=timezone.utc),
                "last_ok_at": datetime(2026, 11, 11, tzinfo=timezone.utc),
            },
            "auth_required",
        ),
        (
            {
                "last_error_code": "upstream_crash",
                "last_error_at": datetime(2026, 11, 12, tzinfo=timezone.utc),
                "last_ok_at": datetime(2026, 11, 11, tzinfo=timezone.utc),
            },
            "unreachable",
        ),
        ({}, "ok"),
    ],
)
def test_derive_status_rules(kwargs, expected):
    assert derive_status(_facts(**kwargs)) == expected


def test_health_fields_null_tool_count_until_probe():
    fields = health_fields(
        _facts(last_probe_at=None, last_error_at=None, last_ok_at=None, enabled_tool_count=0)
    )
    assert fields["status"] == "probing"
    assert fields["tool_count"] is None
    assert fields["token_expires_at"] is None
    assert fields["has_refresh_token"] is False


def test_health_fields_oauth_grant_lifetime():
    expires = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
    fields = health_fields(_facts(token_expires_at=expires, has_refresh_token=True))
    assert fields["token_expires_at"] == "2026-11-15T18:00:00Z"
    assert fields["has_refresh_token"] is True


def test_health_fields_tool_count_after_live_ok_without_probe():
    fields = health_fields(
        _facts(
            last_probe_at=None,
            last_error_at=None,
            last_ok_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
            enabled_tool_count=4,
        )
    )
    assert fields["status"] == "ok"
    assert fields["tool_count"] == 4


def test_should_record_failure_respects_short_timeout_and_skip():
    assert should_record_failure("upstream_timeout") is True
    assert should_record_failure("upstream_timeout", caller_shortened_timeout=True) is False
    assert should_record_failure("upstream_error") is False
    assert should_record_failure(
        "runner_unavailable", message="previous child for this server/key still resolving"
    ) is False
    assert should_record_failure("runner_unavailable", message="runner not connected") is True


def test_success_throttle():
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    assert should_throttle_success(
        status_now="ok",
        last_ok_at=now - timedelta(seconds=10),
        now=now,
    )
    assert not should_throttle_success(
        status_now="unreachable",
        last_ok_at=now - timedelta(seconds=10),
        now=now,
    )
    assert not should_throttle_success(
        status_now="ok",
        last_ok_at=now - timedelta(seconds=90),
        now=now,
    )


class _LiveRunner:
    """Minimal connected runner so stdio servers can derive ``ok``."""

    connected = True
    site_id = "test"
    regions = ("test",)


@pytest.fixture
async def registry(db):
    return Registry(
        async_session,
        _vault(),
        RunnerPool([_LiveRunner()]),
        trusted_path=["/usr/bin", "/bin"],
        binary_allowlist=["/usr/bin/true"],
    )


async def _create_stdio(registry: Registry, slug: str = "demo") -> uuid.UUID:
    view = await registry.create_server(
        name=slug,
        slug=slug,
        kind="stdio",
        command="/usr/bin/true",
        args=[],
        env={},
        actor="test",
    )
    return uuid.UUID(view["id"])


@pytest.mark.anyio
async def test_new_server_is_probing(registry):
    sid = await _create_stdio(registry)
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "probing"
    assert row["tool_count"] is None
    assert row["last_probe_at"] is None


@pytest.mark.anyio
async def test_probe_in_flight_reports_probing(registry):
    sid = await _create_stdio(registry)
    registry._probing.add(sid)
    try:
        rows = await registry.list_servers()
        row = next(r for r in rows if r["id"] == str(sid))
        assert row["status"] == "probing"
    finally:
        registry._probing.discard(sid)


@pytest.mark.anyio
async def test_failed_probe_records_unreachable(registry):
    sid = await _create_stdio(registry)
    # Connected but refuses probe requests → still records the outcome.
    registry.pool = RunnerPool([])
    with pytest.raises(RegistryError) as exc:
        await registry.probe(sid, "test")
    assert exc.value.code == "runner_unavailable"
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "unreachable"
    assert row["tool_count"] is None
    assert row["last_error_code"] == "runner_unavailable"
    assert row["last_error_at"] is not None


@pytest.mark.anyio
async def test_record_outcome_success_and_auth(registry):
    sid = await _create_stdio(registry)
    await registry.record_outcome(sid, None, is_probe=True)
    # Seed a fake tool row so tool_count is non-null after probe.
    async with async_session() as s:
        s.add(
            Tool(
                server_id=sid,
                upstream_name="ping",
                description="",
                schema={"type": "object"},
                schema_hash="abc",
                enabled=True,
            )
        )
        await s.commit()
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "ok"
    assert row["tool_count"] == 1
    assert row["last_probe_at"] is not None

    await registry.record_outcome(sid, "auth_required")
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "auth_required"
    assert row["last_error_code"] == "auth_required"


@pytest.mark.anyio
async def test_shortened_timeout_does_not_change_state(registry):
    sid = await _create_stdio(registry)
    await registry.record_outcome(sid, None, is_probe=True)
    async with async_session() as s:
        before = (await s.execute(select(Server).where(Server.id == sid))).scalar_one()
        err_at = before.last_error_at
        err_code = before.last_error_code
    await registry.record_outcome(
        sid, "upstream_timeout", caller_shortened_timeout=True
    )
    async with async_session() as s:
        after = (await s.execute(select(Server).where(Server.id == sid))).scalar_one()
        assert after.last_error_at == err_at
        assert after.last_error_code == err_code


@pytest.mark.anyio
async def test_upstream_error_ignored(registry):
    sid = await _create_stdio(registry)
    await registry.record_outcome(sid, None, is_probe=True)
    await registry.record_outcome(sid, "upstream_error")
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "ok"
    assert row["last_error_code"] is None


@pytest.mark.anyio
async def test_stdio_unreachable_without_runner(registry):
    sid = await _create_stdio(registry)
    await registry.record_outcome(sid, None, is_probe=True)
    registry.pool = RunnerPool([])
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "unreachable"
    # Reconnect restores the previous ok observation.
    registry.pool = RunnerPool([_LiveRunner()])
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "ok"


@pytest.mark.anyio
async def test_oauth_remote_missing_auth(registry):
    view = await registry.create_server(
        name="oauth-app",
        slug="oauth-app",
        kind="remote",
        command=None,
        args=[],
        env={},
        actor="test",
        url="https://example.com/mcp",
        oauth={
            "authorize_url": "https://idp.example/authorize",
            "token_url": "https://idp.example/token",
            "client_id": "cid",
            "scopes": ["mcp"],
        },
    )
    # Pretend it was probed so credential rule wins over probing.
    sid = uuid.UUID(view["id"])
    await registry.record_outcome(sid, None, is_probe=True)
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "auth_required"
    assert row["has_auth"] is False


@pytest.mark.anyio
async def test_skill_bridge_requires_session(registry):
    async with async_session() as s:
        s.add(
            CatalogApp(
                slug="quark",
                label="Quark",
                name="Quark",
                description="test",
                connection="skill_bridge",
                popular=False,
                categories=[],
                tags=[],
                spec={"manifest": {"name": "quark", "bin": "x", "auth": {"type": "device_qr"}}},
            )
        )
        await s.commit()
    view = await registry.create_server(
        name="Quark",
        slug="quark",
        kind="stdio",
        command="/usr/bin/true",
        args=[],
        env={},
        actor="test",
    )  # binary_allowlist covers true
    sid = uuid.UUID(view["id"])
    await registry.record_outcome(sid, None, is_probe=True)
    rows = await registry.list_servers()
    row = next(r for r in rows if r["id"] == str(sid))
    assert row["status"] == "auth_required"
    assert row["has_skill_session"] is False


@pytest.mark.anyio
async def test_clear_error_and_reprobe_schedules(registry, monkeypatch):
    sid = await _create_stdio(registry)
    await registry.record_outcome(sid, "upstream_crash")
    called: list[uuid.UUID] = []

    async def fake_probe(server_id, actor):
        called.append(server_id)
        return {"server_id": str(server_id), "tools": []}

    monkeypatch.setattr(registry, "probe", fake_probe)
    await registry.clear_error_and_reprobe(sid, "test")
    async with async_session() as s:
        server = (await s.execute(select(Server).where(Server.id == sid))).scalar_one()
        assert server.last_error_code is None
        assert server.last_error_at is None
    await asyncio.sleep(0.05)
    assert called == [sid]
