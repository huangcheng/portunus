"""Agent keys: ``pm-<32 base64url key_id>.<43 base64url secret>``.

Only ``HMAC-SHA256(key=pepper, msg=secret)`` is stored; the full token is returned exactly once at
mint time. Failures raise :class:`AuthError` with the shared ``unauthorized`` / ``revoked`` codes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import select

from portunus_gateway import authz
from portunus_gateway.config import load_pepper
from portunus_gateway.db.models import AgentKey
from portunus_gateway.db.session import async_session
from portunus_shared import PortunusError

TOKEN_PREFIX = "pm-"
_KEY_ID_BYTES = 24  # -> 32 base64url chars
_SECRET_BYTES = 32  # -> 43 base64url chars
_TOKEN_RE = re.compile(r"pm-([A-Za-z0-9_-]{32})\.([A-Za-z0-9_-]{43})")
_ZERO_DIGEST = bytes(32)


class AuthError(PortunusError):
    """Authentication failure; ``code`` is ``unauthorized`` or ``revoked``."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _digest(secret: str) -> bytes:
    return hmac.new(load_pepper(), secret.encode("ascii"), hashlib.sha256).digest()


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise to aware UTC. SQLite round-trips naive datetimes; naive means UTC here."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def mint_agent_key(*, expires_at: datetime | None, label: str | None = None) -> tuple[str, AgentKey]:
    """Create a key. Returns ``(full_token, row)``; the token cannot be recovered afterwards."""
    key_id = _b64url(secrets.token_bytes(_KEY_ID_BYTES))
    secret = _b64url(secrets.token_bytes(_SECRET_BYTES))
    row = AgentKey(key_id=key_id, digest=_digest(secret), label=label, expires_at=_as_utc(expires_at))
    async with async_session() as session:
        session.add(row)
        await session.commit()
    return f"{TOKEN_PREFIX}{key_id}.{secret}", row


async def verify_agent_token(token: str) -> AgentKey:
    """Return the key row for a valid token, else raise :class:`AuthError`.

    Order: format -> lookup -> constant-time digest compare -> revoked -> expired. Revocation and
    expiry are only reported to holders of the correct secret.
    """
    m = _TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
    if m is None:
        raise AuthError("unauthorized", "invalid agent key")
    key_id, secret = m.groups()
    async with async_session() as session:
        row = (await session.execute(select(AgentKey).where(AgentKey.key_id == key_id))).scalar_one_or_none()
    # Always compute the HMAC (even for unknown ids) so lookups don't differ in timing.
    ok = hmac.compare_digest(_digest(secret), row.digest if row is not None else _ZERO_DIGEST)
    if row is None or not ok:
        raise AuthError("unauthorized", "invalid agent key")
    if row.revoked_at is not None or authz.authz_store.is_revoked(key_id):
        raise AuthError("revoked", "agent key revoked")
    expires_at = _as_utc(row.expires_at)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise AuthError("unauthorized", "agent key expired")
    return row


async def revoke_key(key_id: str) -> None:
    """Set ``revoked_at`` (first revoke wins), bump the row generation, bump the authz generation."""
    async with async_session() as session:
        row = (await session.execute(select(AgentKey).where(AgentKey.key_id == key_id))).scalar_one_or_none()
        if row is None:
            raise AuthError("unauthorized", "unknown agent key")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            row.generation += 1
            await session.commit()
    authz.authz_store.mark_revoked(key_id)
