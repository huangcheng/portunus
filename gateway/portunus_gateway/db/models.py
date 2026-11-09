"""SQLAlchemy 2 models for the Portunus registry, vault, keys/grants, sessions, audit."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

SERVER_KINDS = ("stdio", "remote")
SESSION_TTL = timedelta(hours=12)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_expiry() -> datetime:
    return _now() + SESSION_TTL


class Base(DeclarativeBase):
    # Named constraints so Alembic migrations (incl. SQLite batch mode) can reference them.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=_now)


class Server(Base):
    __tablename__ = "servers"
    __table_args__ = (
        CheckConstraint("kind IN ('stdio', 'remote')", name="kind"),
        # Slugs are stored casefolded (see validator); the DB rejects anything else.
        CheckConstraint("slug = lower(slug)", name="slug_lower"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    # Disabled until the operator explicitly enables it.
    enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    command: Mapped[str | None] = mapped_column(String(1024))  # stdio
    args: Mapped[list[str] | None] = mapped_column(JSON)  # stdio argv (literals)
    url: Mapped[str | None] = mapped_column(String(2048))  # remote
    # Soft scheduling affinity: runner regions to try first. Empty = any live runner.
    preferred_regions: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    # Bumped on every vault change; children record it, reconcile compares it.
    vault_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = _created_at()

    @validates("slug")
    def _casefold_slug(self, _key: str, value: str) -> str:
        return value.casefold()


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("server_id", "upstream_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    server_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    upstream_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    schema_hash: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = _created_at()


class VaultEntry(Base):
    """Encrypted secret (env var / header) for a server. See ``vault.Vault`` for AAD binding."""

    __tablename__ = "vault_entries"
    __table_args__ = (UniqueConstraint("server_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    server_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)  # nonce || ct+tag
    key_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class AgentKey(Base):
    """Agent credential ``pm-<key_id>.<secret>``; only HMAC digest of secret is stored."""

    __tablename__ = "agent_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key_id: Mapped[str] = mapped_column(String(64), unique=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary(32))
    label: Mapped[str | None] = mapped_column(String(200))
    # Bumped on revoke; in-flight state tagged with an older generation is invalid.
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL = never expires. SQLite returns naive values on read; treat naive as UTC.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class Grant(Base):
    __tablename__ = "grants"
    __table_args__ = (UniqueConstraint("key_id", "server_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    key_id: Mapped[str] = mapped_column(ForeignKey("agent_keys.key_id", ondelete="CASCADE"))
    server_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = _created_at()


class OperatorSession(Base):
    """Opaque dashboard session; only a hash of the session token is stored."""

    __tablename__ = "operator_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_session_expiry)


class AuditEvent(Base):
    """Metadata-only audit record (30d retention is enforced by a later task)."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _created_at()
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
