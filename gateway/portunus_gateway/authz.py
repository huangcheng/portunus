"""In-process authz snapshot: revoked keys, grants, and a monotonically increasing generation.

The store is a fast, synchronous read path for the request hot path. The database stays the
source of truth; ``reload_from_db`` rebuilds the snapshot. Revocation is monotonic in memory:
a reload never "un-revokes" a key (guards against a reload that read the DB before a revoke
committed). ``has_grant`` deliberately keeps answering for revoked keys -- callers must check
``is_revoked`` / ``verify_agent_token``; ``generation`` lets in-flight work detect that
authorization state changed underneath it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from portunus_gateway.db.models import AgentKey, Grant
from portunus_gateway.db.session import async_session


class AuthzStore:
    def __init__(self) -> None:
        self._revoked: set[str] = set()
        self._grants: set[tuple[str, uuid.UUID]] = set()
        self.generation: int = 0

    def bump(self) -> int:
        self.generation += 1
        return self.generation

    def is_revoked(self, key_id: str) -> bool:
        return key_id in self._revoked

    def has_grant(self, key_id: str, server_id: uuid.UUID) -> bool:
        return (key_id, server_id) in self._grants

    def mark_revoked(self, key_id: str) -> None:
        """Record a revocation; bumps ``generation`` only if the key was not already revoked."""
        if key_id not in self._revoked:
            self._revoked.add(key_id)
            self.bump()

    async def reload_from_db(self) -> None:
        async with async_session() as session:
            revoked = set(
                (await session.execute(select(AgentKey.key_id).where(AgentKey.revoked_at.is_not(None)))).scalars()
            )
            grants = {(k, s) for k, s in (await session.execute(select(Grant.key_id, Grant.server_id))).all()}
        # Single synchronous swap (no awaits): readers never see a half-built snapshot.
        revoked |= self._revoked
        if revoked != self._revoked or grants != self._grants:
            self._revoked, self._grants = revoked, grants
            self.bump()


authz_store = AuthzStore()
