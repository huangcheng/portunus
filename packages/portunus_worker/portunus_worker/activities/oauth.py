from __future__ import annotations

import logging
import uuid

from temporalio import activity

log = logging.getLogger("portunus.worker.activities.oauth")


@activity.defn(name="refresh_oauth")
async def refresh_oauth(server_id: str) -> dict:
    """Refresh access token for a remote server. Returns outcome for the workflow."""
    from portunus_worker.runtime import get_registry

    reg = get_registry()
    sid = uuid.UUID(server_id)
    result = await reg.refresh_oauth_access_token(sid)
    if isinstance(result, tuple):
        ok, expires_in = result
    else:
        ok, expires_in = bool(result), None
    log.info("refresh_oauth server=%s ok=%s expires_in=%s", server_id, ok, expires_in)
    return {"ok": ok, "expires_in": expires_in or 3600}


@activity.defn(name="mark_auth_required")
async def mark_auth_required(server_id: str) -> None:
    from datetime import datetime, timezone

    from portunus_gateway.db.models import Server
    from portunus_gateway.db.session import async_session
    from sqlalchemy import select

    sid = uuid.UUID(server_id)
    async with async_session() as s:
        row = (await s.execute(select(Server).where(Server.id == sid))).scalar_one_or_none()
        if row is None:
            return
        row.last_error_code = "auth_required"
        row.last_error_at = datetime.now(timezone.utc)
        await s.commit()
