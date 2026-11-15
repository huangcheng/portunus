from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from temporalio import activity

log = logging.getLogger("portunus.worker.activities.probe")


@activity.defn(name="list_probe_plan")
async def list_probe_plan(interval_s: int) -> dict:
    """Enabled servers to probe this wave (skip auth_required) + sleep until next scan.

    Servers never probed, or whose ``last_probe_at`` is older than ``interval_s``,
    are due. Fresh successes wait out the interval so a kick does not re-hammer
    everything — ``probe_now`` priority in the workflow still forces one id.
    """
    from sqlalchemy import or_, select

    from portunus_gateway.db.models import Server
    from portunus_gateway.db.session import async_session

    interval = max(60, int(interval_s))
    now = datetime.now(timezone.utc)
    due_before = now - timedelta(seconds=interval)
    async with async_session() as s:
        rows = (
            await s.execute(
                select(Server.id, Server.last_probe_at).where(
                    Server.enabled.is_(True),
                    or_(
                        Server.last_error_code.is_(None),
                        Server.last_error_code != "auth_required",
                    ),
                    or_(
                        Server.last_probe_at.is_(None),
                        Server.last_probe_at <= due_before,
                    ),
                )
            )
        ).all()
        next_ok = (
            await s.execute(
                select(Server.last_probe_at)
                .where(
                    Server.enabled.is_(True),
                    Server.last_probe_at.is_not(None),
                    Server.last_probe_at > due_before,
                    or_(
                        Server.last_error_code.is_(None),
                        Server.last_error_code != "auth_required",
                    ),
                )
                .order_by(Server.last_probe_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    due = [str(r[0]) for r in rows]
    if next_ok is not None:
        next_sleep_s = int((next_ok + timedelta(seconds=interval) - now).total_seconds())
        next_sleep_s = max(30, min(next_sleep_s, 3600))
    else:
        next_sleep_s = interval
    log.info(
        "probe plan due=%d next_sleep_s=%s interval_s=%s",
        len(due),
        next_sleep_s,
        interval,
    )
    return {"due": due, "next_sleep_s": next_sleep_s}


async def _wait_for_runner(reg, *, kind: str, seconds: float = 20.0) -> bool:
    """Stdio probes need a live runner; remotes use RemoteProxy and can proceed immediately."""
    if kind != "stdio":
        return True
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if reg.any_runner_connected():
            return True
        await asyncio.sleep(0.5)
    return reg.any_runner_connected()


@activity.defn(name="probe_server")
async def probe_server(server_id: str) -> dict:
    from portunus_gateway.db.models import Server
    from portunus_gateway.db.session import async_session
    from portunus_worker.runtime import ensure_runner_pool
    from sqlalchemy import select

    sid = uuid.UUID(server_id)
    async with async_session() as s:
        row = (await s.execute(select(Server).where(Server.id == sid))).scalar_one_or_none()
        if row is None:
            return {"skipped": True, "reason": "not_found"}
        if not row.enabled:
            return {"skipped": True, "reason": "disabled"}
        if row.last_error_code == "auth_required":
            return {"skipped": True, "reason": "auth_required"}
        kind = row.kind

    # Prefer gateway-bound Registry (sole-client pool). Standalone falls back to dial+wait.
    from portunus_worker.runtime import gateway_bound

    wait_s = 5.0 if gateway_bound() or kind != "stdio" else 45.0
    reg = await ensure_runner_pool(wait_s=wait_s)
    if kind == "stdio" and not await _wait_for_runner(reg, kind=kind, seconds=5.0):
        log.info("probe_server deferred server=%s reason=runner_not_ready", server_id)
        return {"skipped": True, "reason": "runner_not_ready"}
    try:
        result = await reg.probe(sid, "temporal-probe")
        return {"skipped": False, "ok": True, "tool_count": len(result.get("tools") or [])}
    except Exception as e:  # noqa: BLE001
        # Soft deferrals — workflow sleeps and retries next interval.
        code = getattr(e, "code", None) or ""
        if code in {"child_cap", "probe_in_progress", "runner_unavailable"}:
            log.info("probe_server deferred server=%s reason=%s", server_id, code)
            return {"skipped": True, "reason": code}
        log.warning("probe_server failed server=%s err=%s", server_id, e)
        return {"skipped": False, "ok": False, "error": str(e)}


@activity.defn(name="enable_server_and_tools")
async def enable_server_and_tools(server_id: str) -> dict:
    from portunus_gateway.db.models import Tool
    from portunus_worker.runtime import get_registry
    from sqlalchemy import select

    from portunus_gateway.db.session import async_session

    sid = uuid.UUID(server_id)
    reg = get_registry()
    await reg.set_server_enabled(sid, True, "temporal-connect")
    async with async_session() as s:
        tools = (await s.execute(select(Tool).where(Tool.server_id == sid))).scalars().all()
        ids = [t.id for t in tools]
    for tid in ids:
        await reg.set_tool_enabled(tid, True, "temporal-connect")
    return {"enabled_tools": len(ids)}


@activity.defn(name="ensure_full_grants")
async def ensure_full_grants() -> None:
    from portunus_gateway import authz

    await authz.ensure_full_grants()


@activity.defn(name="start_connect_children")
async def start_connect_children(server_id: str) -> None:
    from portunus_worker.client import request_server_probe

    await request_server_probe(uuid.UUID(server_id), immediate=True)