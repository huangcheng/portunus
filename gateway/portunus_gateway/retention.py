"""Periodic retention for audit_events and tool_calls."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from .db.models import AuditEvent, ToolCall
from .db.session import async_session

log = logging.getLogger("portunus.gateway.retention")


def _days(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


async def purge_once() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    audit_cut = now - timedelta(days=_days("PORTUNUS_AUDIT_RETENTION_DAYS", 30))
    tool_cut = now - timedelta(days=_days("PORTUNUS_TOOL_CALL_RETENTION_DAYS", 14))
    async with async_session() as s:
        a = await s.execute(delete(AuditEvent).where(AuditEvent.created_at < audit_cut))
        t = await s.execute(delete(ToolCall).where(ToolCall.created_at < tool_cut))
        await s.commit()
    return {"audit": max(int(a.rowcount or 0), 0), "tool_calls": max(int(t.rowcount or 0), 0)}


async def retention_loop(stop: asyncio.Event, *, interval_s: float = 3600.0) -> None:
    while not stop.is_set():
        try:
            counts = await purge_once()
            if counts["audit"] or counts["tool_calls"]:
                log.info("retention purged audit=%s tool_calls=%s", counts["audit"], counts["tool_calls"])
        except Exception:  # noqa: BLE001 — never kill the gateway for cleanup
            log.exception("retention purge failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue
