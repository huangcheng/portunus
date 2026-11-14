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
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

SERVER_KINDS = ("stdio", "remote")
CATALOG_CONNECTIONS = ("oauth", "http", "webdav", "stdio", "skill_bridge")
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
    # Operator OAuth config for remotes (authorize/token/client_id/scopes). Secrets stay in vault.
    oauth: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Soft scheduling affinity: runner regions to try first. Empty = any live runner.
    preferred_regions: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    # TOML catalog Connect (nullable = custom / pre-catalog server).
    recipe_slug: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    artifact_snapshot: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    env_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    manifest_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preferred_site_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    artifact_site_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # ensure/Connect health: healthy | degraded | pending_ensure
    health: Mapped[str] = mapped_column(String(32), default="healthy", server_default="healthy")
    health_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Bumped on every vault change; children record it, reconcile compares it.
    vault_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Last successful tools/list probe (null until first success).
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last successful call or probe.
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last failure that counts for derived status (see health.py).
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # Operator-approved JSON Schema; when set, Omni validates/discovers against this
    # instead of the last probed ``schema``. Probe never clears it.
    schema_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # MCP ToolAnnotations (readOnlyHint / destructiveHint / …); null when upstream omitted.
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
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
    """Agent credential ``pm-<key_id>.<secret>``.

    ``digest`` is the HMAC of the secret (auth path). ``secret_ciphertext`` holds the vault-sealed
    full token for operator reveal. Soft-revoke rows are purged; delete removes the key entirely.
    """

    __tablename__ = "agent_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key_id: Mapped[str] = mapped_column(String(64), unique=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary(32))
    label: Mapped[str | None] = mapped_column(String(200))
    # Legacy soft-revoke column; new deletes remove the row. Kept for in-flight/migration.
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL = never expires. SQLite returns naive values on read; treat naive as UTC.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)  # nonce || ct+tag
    secret_key_version: Mapped[int | None] = mapped_column(Integer)
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
    """Metadata-only audit record (retention via PORTUNUS_AUDIT_RETENTION_DAYS)."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _created_at()
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ToolCall(Base):
    """One ``portunus_invoke_tool`` outcome for the Logs page (no args/results)."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_created_at", "created_at"),
        Index("ix_tool_calls_trace_id", "trace_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _created_at()
    trace_id: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    server_slug: Mapped[str] = mapped_column(String(100))
    tool: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(64))
    site_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cold_start: Mapped[bool | None] = mapped_column(nullable=True)
    preferred_miss: Mapped[bool | None] = mapped_column(nullable=True)
    gateway_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    init_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CatalogApp(Base):
    """Operator-editable Apps catalog entry (well-known connect recipes).

    ``connection`` selects the Connect UX; ``spec`` holds the shape-specific fields
    (OAuth URLs, WebDAV root, stdio argv, …). Not a running Server — Connect materializes one.
    """

    __tablename__ = "catalog_apps"
    __table_args__ = (
        CheckConstraint(
            "connection IN ('oauth', 'http', 'webdav', 'stdio', 'skill_bridge')",
            name="connection",
        ),
        CheckConstraint("slug = lower(slug)", name="slug_lower"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    connection: Mapped[str] = mapped_column(String(16))
    popular: Mapped[bool] = mapped_column(default=False, server_default=false())
    categories: Mapped[list[Any]] = mapped_column(JSON, nullable=False, server_default="[]")
    tags: Mapped[list[Any]] = mapped_column(JSON, nullable=False, server_default="[]")
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = _created_at()

    @validates("slug")
    def _casefold_slug(self, _key: str, value: str) -> str:
        return value.casefold()


class SiteHealth(Base):
    """Learned per-(server, runner-site) network health for auto placement/failover.

    Fed from invoke/probe outcomes that name the hosting site. Consecutive network-class
    failures at or above ``SiteHealthPolicy.threshold`` within the TTL make the scheduler
    avoid the site for that server (new children land elsewhere); any success clears it.
    ``preferred_regions`` remains an operator *hint* honored only among healthy sites.
    """

    __tablename__ = "site_health"

    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True
    )
    site_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
