from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from temporalio import activity

log = logging.getLogger("portunus.worker.activities.oauth")


@activity.defn(name="list_oauth_refresh_plan")
async def list_oauth_refresh_plan(skew_s: int) -> dict:
    """Return servers due for refresh and how long to sleep until the next wave.

    ``due``: servers with ``has_oauth_refresh`` whose access token is missing
    expiry or expires within ``skew_s``. Failures for one id never block others —
    the workflow refreshes each in its own activity.
    """
    from sqlalchemy import or_, select

    from portunus_gateway.db.models import Server
    from portunus_gateway.db.session import async_session

    skew = max(30, int(skew_s))
    now = datetime.now(timezone.utc)
    due_before = now + timedelta(seconds=skew)
    async with async_session() as s:
        due_rows = (
            await s.execute(
                select(Server.id).where(
                    Server.has_oauth_refresh.is_(True),
                    or_(
                        Server.token_expires_at.is_(None),
                        Server.token_expires_at <= due_before,
                    ),
                )
            )
        ).all()
        next_expiry = (
            await s.execute(
                select(Server.token_expires_at)
                .where(
                    Server.has_oauth_refresh.is_(True),
                    Server.token_expires_at.is_not(None),
                    Server.token_expires_at > due_before,
                )
                .order_by(Server.token_expires_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    due = [str(r[0]) for r in due_rows]
    if next_expiry is not None:
        # Wake skew seconds before the next expiry.
        next_sleep_s = int((next_expiry - timedelta(seconds=skew) - now).total_seconds())
        next_sleep_s = max(30, min(next_sleep_s, 3600))
    else:
        # Nothing scheduled — poll again so newly stored tokens are noticed.
        next_sleep_s = 300
    log.info(
        "oauth refresh plan due=%d next_sleep_s=%s skew_s=%s",
        len(due),
        next_sleep_s,
        skew,
    )
    return {"due": due, "next_sleep_s": next_sleep_s}


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
