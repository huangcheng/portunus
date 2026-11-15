from __future__ import annotations

import logging
import uuid

from temporalio import activity

log = logging.getLogger("portunus.worker.activities.probe")


@activity.defn(name="probe_server")
async def probe_server(server_id: str) -> dict:
    from portunus_gateway.db.models import Server
    from portunus_gateway.db.session import async_session
    from portunus_worker.runtime import get_registry
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

    reg = get_registry()
    try:
        result = await reg.probe(sid, "temporal-probe")
        return {"skipped": False, "ok": True, "tool_count": len(result.get("tools") or [])}
    except Exception as e:  # noqa: BLE001
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
    import uuid

    from portunus_worker.client import start_server_probe

    await start_server_probe(uuid.UUID(server_id))


@activity.defn(name="start_connect_children")
async def start_connect_children(server_id: str) -> None:
    import uuid

    from portunus_worker.client import start_server_probe

    await start_server_probe(uuid.UUID(server_id))
