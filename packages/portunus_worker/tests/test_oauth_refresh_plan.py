"""list_oauth_refresh_plan must include paused (enabled=false) Apps."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.anyio


async def _plan_with_servers(monkeypatch, rows: list[dict]) -> dict:
    from portunus_gateway.db.models import Base, Server
    from portunus_worker.activities import oauth as oauth_acts

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async with sf() as s:
        for row in rows:
            s.add(
                Server(
                    id=row["id"],
                    name=row["name"],
                    slug=row["slug"],
                    kind="remote",
                    url="https://example.com/mcp",
                    enabled=row["enabled"],
                    has_oauth_refresh=row["has_oauth_refresh"],
                    token_expires_at=row.get("token_expires_at"),
                    oauth={"client_id": "x"},
                )
            )
        await s.commit()

    # Activity does `from portunus_gateway.db.session import async_session` at call time.
    import portunus_gateway.db.session as session_mod

    monkeypatch.setattr(session_mod, "async_session", sf)

    return await oauth_acts.list_oauth_refresh_plan(300)


async def test_paused_app_with_due_token_is_in_refresh_plan(monkeypatch):
    paused_id = uuid.uuid4()
    live_id = uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)

    plan = await _plan_with_servers(
        monkeypatch,
        [
            {
                "id": paused_id,
                "name": "Paused OAuth",
                "slug": "paused-oauth",
                "enabled": False,
                "has_oauth_refresh": True,
                "token_expires_at": past,
            },
            {
                "id": live_id,
                "name": "Live OAuth",
                "slug": "live-oauth",
                "enabled": True,
                "has_oauth_refresh": True,
                "token_expires_at": future,
            },
        ],
    )

    due = set(plan["due"])
    assert str(paused_id) in due
    assert str(live_id) not in due  # still outside skew window
    # Sleep should track the live app's future expiry (not ignore paused-only due).
    assert 30 <= int(plan["next_sleep_s"]) <= 3600


async def test_paused_app_future_expiry_still_drives_next_sleep(monkeypatch):
    """Paused Apps must participate in next_sleep scheduling, not only the due set."""
    paused_id = uuid.uuid4()
    soon = datetime.now(timezone.utc) + timedelta(minutes=20)

    plan = await _plan_with_servers(
        monkeypatch,
        [
            {
                "id": paused_id,
                "name": "Paused Soon",
                "slug": "paused-soon",
                "enabled": False,
                "has_oauth_refresh": True,
                "token_expires_at": soon,
            },
        ],
    )

    assert plan["due"] == []
    # skew=300 → wake ~15m before expiry → about 5 minutes from now (clamped).
    assert 30 <= int(plan["next_sleep_s"]) <= 3600
