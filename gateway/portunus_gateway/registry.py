"""Servers/tools registry: create stdio servers, probe them through the runner, enable tools.

Invariants
----------
* Everything new is **disabled**: servers, and every tool a probe discovers. Only an explicit
  operator action enables either.
* The closed argv grammar is validated on create *and again at probe time* (policy can change
  between the two); the runner re-validates before ``exec`` and stays authoritative.
* Env literals live only in the vault (AES-GCM, AAD-bound to the entry id). They are decrypted
  just-in-time for a probe and never returned by any API or written to the audit log.
* A probe is an ephemeral runner child in the ``probe`` pool: at most ``max_probes`` at once,
  one per server, outside the invoke slot FSM and its caps. A re-probe that finds a tool's
  schema or description changed disables that tool again (it must be re-approved); tools that
  disappeared are kept (invoke reports ``tool_missing``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

# oauth_link browser_capture jobs live in runner memory — status polls must hit the same site.
_OAUTH_JOB_SITES: dict[str, tuple[str, float]] = {}  # job_id -> (site_id, expires_at)
_OAUTH_JOB_SITE_TTL_S = 300.0

from portunus_shared import ALLOWED_PORTUNUS_ENV, DEFAULTS, PortunusError, validate_stdio_command
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditEvent, CatalogApp, Server, Tool, VaultEntry
from .health import (
    HealthFacts,
    health_fields,
    should_record_failure,
    should_throttle_success,
)
from .oauth import (
    AUTH_HEADER_NAME,
    OAUTH_CLIENT_SECRET_NAME,
    OAuthError,
    oauth_public_view,
    validate_oauth_config,
)
from .runner_client import RunnerRequestError
from .runner_pool import RunnerPool
from .vault import Vault, VaultError

log = logging.getLogger("portunus.gateway.registry")

MAX_ENV_ENTRIES = 256  # mirrors the runner's bound
MAX_ENV_BYTES = 128 * 1024
MAX_TOOLS = 2000
MAX_PREFERRED_REGIONS = 16
PROBE_TRANSPORT_SLACK_S = 5  # the runner enforces timeout_s; leave room for its reply
_REGION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", re.ASCII)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# HTTP header names for remote vault entries (token / custom headers).
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", re.ASCII)
_FORBIDDEN_ENV_PREFIXES = ("PORTUNUS_", "LD_", "DYLD_")  # runner rejects these too
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer"}
)
_RUNNER_STATUS = {
    "runner_unavailable": 503,
    "upstream_timeout": 504,
    "spawn_rejected": 422,
    "spawn_failed": 502,
    "upstream_crash": 502,
    "payload_too_large": 502,
    "auth_required": 401,
    "child_cap": 429,
}


class RegistryError(Exception):
    """An API-level failure: HTTP ``status`` plus a machine-readable ``code``."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.status = status
        self.code = code
        self.message = message


class _Runner(Protocol):
    async def request(
        self, op: str, *, timeout: float, conn_seq: int | None = None, **fields: Any
    ) -> dict: ...


def schema_hash(schema: dict) -> str:
    canon = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def validate_env(env: dict[str, str]) -> dict[str, str]:
    if not isinstance(env, dict) or len(env) > MAX_ENV_ENTRIES:
        raise RegistryError(422, "invalid_env", "env must be an object of string literals")
    total = 0
    for k, v in env.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RegistryError(422, "invalid_env", "env must map strings to strings")
        if not _ENV_NAME.fullmatch(k):
            raise RegistryError(422, "invalid_env", f"invalid env name {k!r}")
        uk = k.upper()
        if uk.startswith(_FORBIDDEN_ENV_PREFIXES) and uk not in ALLOWED_PORTUNUS_ENV:
            raise RegistryError(422, "invalid_env", f"env name not allowed: {k}")
        if "\x00" in v:
            raise RegistryError(422, "invalid_env", "NUL in env value")
        total += len(k) + len(v)
    if total > MAX_ENV_BYTES:
        raise RegistryError(422, "invalid_env", "env too large")
    return dict(env)


def validate_headers(headers: dict[str, str]) -> dict[str, str]:
    """Vault-backed request headers for remote servers (Authorization, X-*, …)."""
    if not isinstance(headers, dict) or len(headers) > MAX_ENV_ENTRIES:
        raise RegistryError(422, "invalid_env", "headers must be an object of string literals")
    total = 0
    out: dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RegistryError(422, "invalid_env", "headers must map strings to strings")
        if not _HEADER_NAME.fullmatch(k) or k.lower() in _FORBIDDEN_HEADERS:
            raise RegistryError(422, "invalid_env", f"invalid header name {k!r}")
        if k.lower().startswith("portunus."):
            raise RegistryError(422, "invalid_env", f"header name not allowed: {k}")
        if "\r" in v or "\n" in v or "\x00" in v:
            raise RegistryError(422, "invalid_env", "invalid header value")
        total += len(k) + len(v)
        out[k] = v
    if total > MAX_ENV_BYTES:
        raise RegistryError(422, "invalid_env", "headers too large")
    return out


def validate_remote_url(url: str | None) -> str:
    if not isinstance(url, str) or not url.strip():
        raise RegistryError(422, "invalid_url", "remote server needs a url")
    url = url.strip()
    if len(url) > 2048:
        raise RegistryError(422, "invalid_url", "url too long")
    # Full SSRF checks run at probe/invoke time; create only rejects obvious junk.
    low = url.lower()
    if not (low.startswith("https://") or low.startswith("http://")):
        raise RegistryError(422, "invalid_url", "url must be http(s)")
    if "\x00" in url or "\r" in url or "\n" in url:
        raise RegistryError(422, "invalid_url", "invalid url")
    return url


def validate_regions(regions: Any) -> list[str]:
    """Normalize a ``preferred_regions`` list: strict names, de-duplicated, order preserved."""
    if not isinstance(regions, list) or len(regions) > MAX_PREFERRED_REGIONS:
        raise RegistryError(422, "invalid_regions", "preferred_regions must be a short list")
    out: list[str] = []
    for r in regions:
        if not isinstance(r, str) or not _REGION.fullmatch(r):
            raise RegistryError(422, "invalid_regions", "invalid region name")
        if r not in out:
            out.append(r)
    return out


def server_view(
    server: Server, env_names: list[str], *, health: dict[str, Any] | None = None
) -> dict[str, Any]:
    # Hide reserved vault names from the operator UI.
    public_names = sorted(n for n in env_names if not n.lower().startswith("portunus."))
    out: dict[str, Any] = {
        "id": str(server.id),
        "name": server.name,
        "slug": server.slug,
        "kind": server.kind,
        "enabled": server.enabled,
        "command": server.command,
        "args": list(server.args or []),
        "url": server.url,
        "oauth": oauth_public_view(server.oauth if isinstance(server.oauth, dict) else None),
        # Any non-reserved vault entry counts (Authorization, WyHeader, …).
        "has_auth": any(not n.lower().startswith("portunus.") for n in env_names),
        # device_qr marker written after platform login succeeds.
        "has_skill_session": any(n == "SKILL_SESSION" for n in env_names),
        "preferred_regions": list(server.preferred_regions or []),
        "vault_version": server.vault_version,
        "env_names": public_names,  # names only, never values
        "created_at": server.created_at.isoformat() if server.created_at else None,
    }
    if health:
        out.update(health)
    return out


def tool_view(tool: Tool) -> dict[str, Any]:
    return {
        "id": str(tool.id),
        "server_id": str(tool.server_id),
        "name": tool.upstream_name,
        "description": tool.description,
        "schema": tool.schema,
        "schema_hash": tool.schema_hash,
        "enabled": tool.enabled,
    }


def _normalize_tools(raw: Any) -> list[dict[str, Any]]:
    """Validate the runner's ``tools`` payload. All-or-nothing: one bad entry rejects it all."""

    def bad(why: str) -> RegistryError:
        return RegistryError(502, "upstream_crash", f"malformed tools/list result: {why}")

    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        raise bad("not a list")
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise bad("tool is not an object")
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 200 or "\x00" in name:
            raise bad("bad tool name")
        if name in out:
            raise bad("duplicate tool name")
        desc = item.get("description")
        if desc is not None and (not isinstance(desc, str) or "\x00" in desc):
            raise bad("bad description")
        schema = item.get("inputSchema")
        if schema is None:
            schema = {"type": "object"}
        if not isinstance(schema, dict):
            raise bad("inputSchema is not an object")
        out[name] = {"name": name, "description": desc, "schema": schema}
    return list(out.values())


class Registry:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        vault: Vault,
        runner: _Runner,
        *,
        trusted_path: list[str],
        binary_allowlist: list[str],
        slots: Any = None,
        remote: Any = None,
        probe_timeout_s: int = DEFAULTS.probe_timeout_s,
        max_probes: int = DEFAULTS.max_probes,
    ) -> None:
        self._sf = session_factory
        self.vault = vault
        self.runner = runner
        self.pool = RunnerPool.ensure(runner)
        self.trusted_path = list(trusted_path)
        self.binary_allowlist = list(binary_allowlist)
        self.slots = slots  # SlotManager (disable_server) once wired by the app
        self.remote = remote  # RemoteProxy once wired; required for remote probe/invoke
        self.probe_timeout_s = probe_timeout_s
        self.max_probes = max_probes
        self._probing: set[uuid.UUID] = set()
        self._probe_tasks: set[asyncio.Task[Any]] = set()

    @property
    def session_factory(self) -> Callable[[], AsyncSession]:
        return self._sf

    def is_probing(self, server_id: uuid.UUID) -> bool:
        return server_id in self._probing

    def any_runner_connected(self) -> bool:
        return self.pool.any_connected()

    # ---- health observations -----------------------------------------------------

    async def _skill_bridge_slugs(self, s: AsyncSession) -> set[str]:
        rows = (
            await s.execute(
                select(CatalogApp.slug).where(CatalogApp.connection == "skill_bridge")
            )
        ).scalars()
        return {str(slug) for slug in rows}

    async def _enabled_tool_counts(
        self, s: AsyncSession, server_ids: list[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, int]:
        stmt = (
            select(Tool.server_id, func.count())
            .where(Tool.enabled.is_(True))
            .group_by(Tool.server_id)
        )
        if server_ids is not None:
            if not server_ids:
                return {}
            stmt = stmt.where(Tool.server_id.in_(server_ids))
        counts: dict[uuid.UUID, int] = {}
        for sid, n in (await s.execute(stmt)).all():
            counts[sid] = int(n)
        return counts

    def _facts_for(
        self,
        server: Server,
        env_names: list[str],
        *,
        requires_skill_session: bool,
        enabled_tool_count: int,
    ) -> HealthFacts:
        return HealthFacts(
            kind=server.kind,
            oauth=server.oauth if isinstance(server.oauth, dict) else None,
            has_authorization=any(n.lower() == AUTH_HEADER_NAME.lower() for n in env_names),
            requires_skill_session=requires_skill_session,
            has_skill_session=any(n == "SKILL_SESSION" for n in env_names),
            probing=self.is_probing(server.id),
            runner_connected=self.any_runner_connected(),
            last_probe_at=server.last_probe_at,
            last_ok_at=server.last_ok_at,
            last_error_code=server.last_error_code,
            last_error_at=server.last_error_at,
            enabled_tool_count=enabled_tool_count,
        )

    async def health_for_server(
        self, server: Server, env_names: list[str], *, enabled_tool_count: int | None = None
    ) -> dict[str, Any]:
        async with self._sf() as s:
            skill_slugs = await self._skill_bridge_slugs(s)
            if enabled_tool_count is None:
                counts = await self._enabled_tool_counts(s, [server.id])
                enabled_tool_count = counts.get(server.id, 0)
        facts = self._facts_for(
            server,
            env_names,
            requires_skill_session=server.slug in skill_slugs,
            enabled_tool_count=enabled_tool_count,
        )
        return health_fields(facts)

    async def _enriched_view(self, server: Server, env_names: list[str]) -> dict[str, Any]:
        health = await self.health_for_server(server, env_names)
        return server_view(server, env_names, health=health)

    async def record_outcome(
        self,
        server_id: uuid.UUID,
        code: str | None,
        *,
        is_probe: bool = False,
        message: str = "",
        caller_shortened_timeout: bool = False,
    ) -> None:
        """Persist a call/probe observation. ``code is None`` means success.

        Best-effort: DB failures are logged and swallowed so bookkeeping never
        masks the invoke/probe outcome the caller already has.
        """
        try:
            await self._record_outcome(
                server_id,
                code,
                is_probe=is_probe,
                message=message,
                caller_shortened_timeout=caller_shortened_timeout,
            )
        except Exception:  # noqa: BLE001 — never fail the call/probe path
            log.exception("record_outcome failed for %s code=%s", server_id, code)

    async def _record_outcome(
        self,
        server_id: uuid.UUID,
        code: str | None,
        *,
        is_probe: bool = False,
        message: str = "",
        caller_shortened_timeout: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        if code is None:
            async with self._sf() as s:
                server = await s.get(Server, server_id)
                if server is None:
                    return
                # Cheap early-out: already ok recently → writing last_ok_at cannot change status.
                if (
                    not is_probe
                    and server.last_ok_at is not None
                    and (server.last_error_at is None or server.last_ok_at >= server.last_error_at)
                    and should_throttle_success(
                        status_now="ok", last_ok_at=server.last_ok_at, now=now
                    )
                ):
                    return
                names = await self._env_names(s, server_id)
                skill_slugs = await self._skill_bridge_slugs(s)
                counts = await self._enabled_tool_counts(s, [server_id])
                facts = self._facts_for(
                    server,
                    names,
                    requires_skill_session=server.slug in skill_slugs,
                    enabled_tool_count=counts.get(server_id, 0),
                )
                status_now = health_fields(facts)["status"]
                if not is_probe and should_throttle_success(
                    status_now=status_now, last_ok_at=server.last_ok_at, now=now
                ):
                    return
                server.last_ok_at = now
                if is_probe:
                    server.last_probe_at = now
                await s.commit()
            return

        if not should_record_failure(
            code, message=message, caller_shortened_timeout=caller_shortened_timeout
        ):
            return
        async with self._sf() as s:
            server = await s.get(Server, server_id)
            if server is None:
                return
            server.last_error_code = code
            server.last_error_at = now
            await s.commit()

    async def health_by_server_ids(
        self, servers: list[Server]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        """Batch health fields for discovery / list views (one shared derivation)."""
        if not servers:
            return {}
        ids = [sv.id for sv in servers]
        async with self._sf() as s:
            names: dict[uuid.UUID, list[str]] = {}
            for sid, n in (
                await s.execute(
                    select(VaultEntry.server_id, VaultEntry.name).where(
                        VaultEntry.server_id.in_(ids)
                    )
                )
            ).all():
                names.setdefault(sid, []).append(n)
            skill_slugs = await self._skill_bridge_slugs(s)
            counts = await self._enabled_tool_counts(s, ids)
        out: dict[uuid.UUID, dict[str, Any]] = {}
        for sv in servers:
            facts = self._facts_for(
                sv,
                names.get(sv.id, []),
                requires_skill_session=sv.slug in skill_slugs,
                enabled_tool_count=counts.get(sv.id, 0),
            )
            out[sv.id] = health_fields(facts)
        return out

    async def clear_error_and_reprobe(self, server_id: uuid.UUID, actor: str) -> None:
        """Credential change: clear last_error_* and schedule a probe."""
        async with self._sf() as s:
            server = await s.get(Server, server_id)
            if server is None:
                return
            server.last_error_code = None
            server.last_error_at = None
            await s.commit()
        self._schedule_probe(server_id, actor)

    def _schedule_probe(self, server_id: uuid.UUID, actor: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run() -> None:
            try:
                await self.probe(server_id, actor)
            except RegistryError as e:
                log.info("post-credential probe failed for %s: %s", server_id, e.code)
            except Exception:  # noqa: BLE001 — never fail the credential write path
                log.exception("post-credential probe crashed for %s", server_id)

        task = loop.create_task(_run(), name=f"portunus-probe-{server_id}")
        self._probe_tasks.add(task)
        task.add_done_callback(self._probe_tasks.discard)

    # ---- argv --------------------------------------------------------------------

    def _validate_argv(self, command: str | None, args: list[str]) -> None:
        if not command:
            raise RegistryError(422, "spawn_rejected", "stdio server needs a command")
        try:
            validate_stdio_command(
                command,
                args,
                trailing_allowlist=[],  # not persisted: dash-args beyond the grammar are refused
                trusted_path=self.trusted_path,
                binary_allowlist=self.binary_allowlist,
            )
        except PortunusError as e:
            raise RegistryError(422, e.code, e.message) from e

    # ---- servers -----------------------------------------------------------------

    async def create_server(
        self,
        *,
        name: str,
        slug: str,
        kind: str,
        command: str | None,
        args: list[str],
        env: dict[str, str],
        actor: str,
        preferred_regions: list[str] | None = None,
        url: str | None = None,
        oauth: dict[str, Any] | None = None,
        oauth_client_secret: str | None = None,
    ) -> dict[str, Any]:
        if kind not in ("stdio", "remote"):
            raise RegistryError(400, "unsupported", f"server kind {kind!r} is not supported")
        regions = validate_regions(preferred_regions or [])
        try:
            oauth_cfg = validate_oauth_config(oauth)
        except OAuthError as e:
            raise RegistryError(e.status, e.code, e.message) from e
        secrets: dict[str, str]
        if kind == "stdio":
            self._validate_argv(command, args)
            secrets = validate_env(env)
            if url or oauth_cfg or oauth_client_secret:
                raise RegistryError(422, "invalid_request", "stdio servers do not take url/oauth")
            server = Server(
                id=uuid.uuid4(),
                name=name,
                slug=slug,
                kind="stdio",
                enabled=False,
                command=command,
                args=list(args),
                preferred_regions=regions,
                vault_version=1 if secrets else 0,
            )
        else:
            if command or args:
                raise RegistryError(422, "invalid_request", "remote servers do not take command/args")
            remote_url = validate_remote_url(url)
            secrets = validate_headers(env)
            if oauth_client_secret is not None:
                if not isinstance(oauth_client_secret, str) or "\x00" in oauth_client_secret:
                    raise RegistryError(422, "invalid_oauth", "invalid oauth_client_secret")
                if oauth_client_secret:
                    secrets = {**secrets, OAUTH_CLIENT_SECRET_NAME: oauth_client_secret}
            server = Server(
                id=uuid.uuid4(),
                name=name,
                slug=slug,
                kind="remote",
                enabled=False,
                url=remote_url,
                oauth=oauth_cfg,
                preferred_regions=regions,
                vault_version=1 if secrets else 0,
            )
        entries = [self._seal_entry(server.id, key, value) for key, value in secrets.items()]
        async with self._sf() as s:
            try:
                s.add(server)
                await s.flush()  # server row first: vault entries reference it
                s.add_all(entries)
                s.add(
                    self._audit(
                        actor,
                        "server.create",
                        server.id,
                        {"slug": server.slug, "kind": server.kind},
                    )
                )
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise RegistryError(409, "conflict", "slug already in use") from None
        return await self._enriched_view(server, list(secrets))

    async def update_server(
        self,
        server_id: uuid.UUID,
        *,
        actor: str,
        preferred_regions: list[str] | None = None,
        oauth: dict[str, Any] | None = None,
        oauth_client_secret: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if (
            preferred_regions is None
            and oauth is None
            and oauth_client_secret is None
            and env is None
        ):
            raise RegistryError(422, "invalid_request", "no fields to update")
        regions = validate_regions(preferred_regions) if preferred_regions is not None else None
        try:
            oauth_cfg = validate_oauth_config(oauth) if oauth is not None else None
        except OAuthError as e:
            raise RegistryError(e.status, e.code, e.message) from e
        if oauth_client_secret is not None:
            if not isinstance(oauth_client_secret, str) or "\x00" in oauth_client_secret:
                raise RegistryError(422, "invalid_oauth", "invalid oauth_client_secret")
        secrets: dict[str, str] | None = None
        if env is not None:
            # Decided after we know server.kind (stdio env vs remote headers).
            secrets = dict(env)
        creds_changed = bool(secrets) or bool(oauth_client_secret)
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if (oauth is not None or oauth_client_secret is not None) and server.kind != "remote":
                raise RegistryError(422, "invalid_request", "stdio servers do not take oauth")
            if secrets is not None:
                secrets = (
                    validate_env(secrets) if server.kind == "stdio" else validate_headers(secrets)
                )
            audit: dict[str, Any] = {}
            if regions is not None:
                server.preferred_regions = regions
                audit["preferred_regions"] = regions
            if oauth is not None:
                # Empty object clears oauth; non-empty replaces config.
                server.oauth = oauth_cfg
                audit["oauth"] = bool(oauth_cfg)
            if oauth_client_secret:
                await self._upsert_vault(s, server, OAUTH_CLIENT_SECRET_NAME, oauth_client_secret)
                audit["oauth_client_secret"] = True
            if secrets:
                for key, value in secrets.items():
                    await self._upsert_vault(s, server, key, value)
                audit["env"] = sorted(secrets)
            s.add(self._audit(actor, "server.update", server_id, audit or None))
            await s.commit()
            names = await self._env_names(s, server_id)
        if creds_changed:
            await self.clear_error_and_reprobe(server_id, actor)
        server = await self._load_server(server_id)
        return await self._enriched_view(server, names)

    async def list_servers(self) -> list[dict[str, Any]]:
        async with self._sf() as s:
            servers = list(
                (await s.execute(select(Server).order_by(Server.created_at))).scalars().all()
            )
            names: dict[uuid.UUID, list[str]] = {}
            rows = await s.execute(select(VaultEntry.server_id, VaultEntry.name))
            for sid, n in rows:
                names.setdefault(sid, []).append(n)
        health_map = await self.health_by_server_ids(servers)
        return [
            server_view(sv, names.get(sv.id, []), health=health_map[sv.id]) for sv in servers
        ]

    async def list_tools(self, server_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._sf() as s:
            await self._get_server(s, server_id)
            tools = (
                (
                    await s.execute(
                        select(Tool).where(Tool.server_id == server_id).order_by(Tool.upstream_name)
                    )
                )
                .scalars()
                .all()
            )
        return [tool_view(t) for t in tools]

    async def set_server_enabled(
        self, server_id: uuid.UUID, enabled: bool, actor: str
    ) -> dict[str, Any]:
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            server.enabled = enabled
            s.add(
                self._audit(
                    actor, "server.enable" if enabled else "server.disable", server_id, None
                )
            )
            await s.commit()
            names = await self._env_names(s, server_id)
        view = await self._enriched_view(server, names)
        if not enabled and self.slots is not None:
            self.slots.disable_server(server_id)  # children -> pending_stop, killed when reachable
        return view

    async def set_tool_enabled(self, tool_id: uuid.UUID, enabled: bool, actor: str) -> dict[str, Any]:
        async with self._sf() as s:
            tool = await s.get(Tool, tool_id)
            if tool is None:
                raise RegistryError(404, "not_found", "tool not found")
            tool.enabled = enabled
            s.add(
                self._audit(
                    actor,
                    "tool.enable" if enabled else "tool.disable",
                    tool_id,
                    {"server_id": str(tool.server_id), "name": tool.upstream_name},
                )
            )
            await s.commit()
            return tool_view(tool)

    # ---- invoke ------------------------------------------------------------------

    async def start_payload(self, server_id: uuid.UUID) -> dict[str, Any]:
        """The ``start`` payload for an invoke child of this server (``SlotManager`` shape).

        Argv is re-validated (policy may have changed since create) and env literals are
        decrypted just-in-time; the result goes straight to the runner and is never logged.
        """
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if server.kind != "stdio":
                raise RegistryError(400, "unsupported", "only stdio servers can be started")
            command, args = server.command, list(server.args or [])
            self._validate_argv(command, args)
            env = await self._decrypt_env(s, server_id)
            env = await self._inject_skill_bridge_manifest(s, server.slug, env)
            vault_version = server.vault_version
            preferred_regions = list(server.preferred_regions or [])
        assert command is not None
        return {
            "argv": [command, *args],
            "trailing_allowlist": [],
            "env": env,
            "vault_version": vault_version,
            "preferred_regions": preferred_regions,  # scheduling hint; not sent to the runner
        }

    async def _inject_skill_bridge_manifest(
        self, s: AsyncSession, slug: str, env: dict[str, str]
    ) -> dict[str, str]:
        """Overlay live catalog manifest for skill_bridge apps (DB hot-update source of truth)."""
        from .catalog import SKILL_BRIDGE_MANIFEST_ENV
        from .db.models import CatalogApp

        row = (
            await s.execute(select(CatalogApp).where(CatalogApp.slug == slug.casefold()))
        ).scalar_one_or_none()
        if row is None or row.connection != "skill_bridge":
            return env
        manifest = (row.spec or {}).get("manifest")
        if not isinstance(manifest, dict):
            return env
        out = dict(env)
        out[SKILL_BRIDGE_MANIFEST_ENV] = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        return out

    # ---- probe -------------------------------------------------------------------

    async def probe(self, server_id: uuid.UUID, actor: str) -> dict[str, Any]:
        # Reserve before any await: check + add are atomic on the event loop.
        if server_id in self._probing:
            raise RegistryError(409, "probe_in_progress", "a probe for this server is running")
        if len(self._probing) >= self.max_probes:
            raise RegistryError(429, "child_cap", "probe pool busy")
        self._probing.add(server_id)
        try:
            return await self._probe(server_id, actor)
        except RegistryError as e:
            await self.record_outcome(server_id, e.code, is_probe=True, message=e.message)
            raise
        finally:
            self._probing.discard(server_id)

    async def skill_auth(
        self,
        server_id: uuid.UUID,
        action: str,
        actor: str,
        *,
        token: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Platform-owned skill CLI auth (device_qr / oauth_link). Not agent MCP."""
        from portunus_skill_bridge.manifest import Manifest

        logical = action.strip()
        aliases = {
            "login-status": "status",
            "refresh-login": "refresh",
        }
        logical = aliases.get(logical, logical)
        if logical not in {"login", "status", "logout", "relogin", "refresh"}:
            raise RegistryError(422, "invalid_request", f"unsupported skill auth action: {action}")
        token_val = token.strip() if isinstance(token, str) else ""
        job_val = job_id.strip() if isinstance(job_id, str) else ""
        if token_val and logical != "login":
            raise RegistryError(422, "invalid_request", "token is only valid for login")

        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if server.kind != "stdio":
                raise RegistryError(422, "unsupported", "skill auth requires a stdio skill-bridge server")
            env = await self._decrypt_env(s, server_id)
            skill_root = env.get("SKILL_ROOT")
            state_dir = env.get("STATE_DIR")
            entrypoint = ""
            login_env: dict[str, str] = {}
            qr_path_field = "qr_code_path"
            agent_name: str | None = None
            agent_profile_dir: str | None = None
            token_flag = "--token"
            success_code = 0
            pending_codes: list[int] = [-114]
            oauth_link = False
            device_qr = False
            browser_capture = False
            capture_timeout_s = 90.0
            catalog = (
                await s.execute(select(CatalogApp).where(CatalogApp.slug == server.slug.casefold()))
            ).scalar_one_or_none()
            if catalog is None or catalog.connection != "skill_bridge":
                raise RegistryError(422, "unsupported", "skill auth requires a skill_bridge catalog app")
            defaults = (catalog.spec or {}).get("env_defaults") or {}
            if isinstance(defaults, dict):
                skill_root = skill_root or defaults.get("SKILL_ROOT")
                state_dir = state_dir or defaults.get("STATE_DIR")
            try:
                manifest = Manifest.model_validate((catalog.spec or {}).get("manifest") or {})
            except Exception as e:
                raise RegistryError(422, "invalid_request", f"invalid skill manifest: {e}") from e
            if not manifest.auth.is_platform_auth():
                raise RegistryError(
                    422,
                    "unsupported",
                    f"skill auth requires device_qr or oauth_link (got {manifest.auth.type!r})",
                )
            device_qr = manifest.auth.is_device_qr()
            oauth_link = manifest.auth.is_oauth_link()
            browser_capture = manifest.auth.wants_browser_capture()
            capture_timeout_s = float(manifest.auth.capture_timeout_s)
            login_env = dict(manifest.auth.login_env)
            qr_path_field = manifest.auth.qr_path_field
            token_flag = manifest.auth.token_flag
            success_code = int(manifest.auth.success_code)
            pending_codes = [int(c) for c in manifest.auth.pending_codes]
            acts = manifest.auth.actions
            cli_action = {
                "login": acts.login,
                "status": acts.status,
                "logout": acts.logout,
                "relogin": acts.relogin,
                "refresh": acts.refresh,
            }[logical]
            if manifest.cli is not None:
                agent_name = manifest.cli.identity.agent_name
                agent_profile_dir = manifest.cli.identity.agent_profile_dir
                if agent_profile_dir:
                    agent_profile_dir = agent_profile_dir.replace("${STATE_DIR}", state_dir or "")
                    agent_profile_dir = agent_profile_dir.replace("${SKILL_ROOT}", skill_root or "")
                cmd = manifest.cli.command
                if len(cmd) >= 2 and isinstance(cmd[-1], str) and not str(cmd[-1]).startswith("/"):
                    entrypoint = str(cmd[-1])
            preferred_regions = list(server.preferred_regions or [])

        if not skill_root or not state_dir:
            raise RegistryError(
                422,
                "invalid_request",
                "SKILL_ROOT and STATE_DIR are required (connect with env_defaults first)",
            )
        if not entrypoint:
            raise RegistryError(422, "invalid_request", "manifest.cli.command must end with a relative entrypoint")

        extra_args: list[str] = []
        if token_val:
            extra_args = [token_flag, token_val]

        # oauth_link + browser_capture: keep CLI alive while Connect opens URL / polls job.
        capture_login = browser_capture and logical == "login" and not token_val
        timeout_s = capture_timeout_s if capture_login else 60.0
        timeout_s = max(15.0, min(timeout_s, 300.0))

        # Status polls for a capture job must stick to the runner that owns it.
        now = time.monotonic()
        for jid, (_site, exp) in list(_OAUTH_JOB_SITES.items()):
            if exp <= now:
                _OAUTH_JOB_SITES.pop(jid, None)

        pinned_site: str | None = None
        if browser_capture and logical == "status" and job_val:
            pinned = _OAUTH_JOB_SITES.get(job_val)
            if pinned is not None:
                pinned_site = pinned[0]

        if pinned_site is not None:
            if not self.pool.is_connected(pinned_site):
                _OAUTH_JOB_SITES.pop(job_val, None)
                raise RegistryError(
                    503, "runner_unavailable", f"oauth job runner {pinned_site!r} not connected"
                )
            site_id = pinned_site
        else:
            decision = self.pool.pick(preferred_regions)
            if decision is None:
                raise RegistryError(503, "runner_unavailable", "runner not connected")
            site_id = decision.site_id

        req: dict[str, Any] = {
            "skill_root": skill_root,
            "state_dir": state_dir,
            "entrypoint": entrypoint,
            "action": cli_action,
            "env": login_env,
            "qr_path_field": qr_path_field,
            "extra_args": extra_args,
            "timeout_s": timeout_s,
        }
        if agent_name:
            req["agent_name"] = agent_name
        if agent_profile_dir:
            req["agent_profile_dir"] = agent_profile_dir
        if capture_login:
            req["oauth_capture"] = True
        if browser_capture and logical == "status" and job_val:
            # Poll background capture job instead of get-user-info.
            req = {
                "skill_root": skill_root,
                "state_dir": state_dir,
                "entrypoint": entrypoint,
                "action": "login",  # unused when oauth_poll
                "oauth_job_id": job_val,
                "oauth_poll": True,
                "env": {},
                "extra_args": [],
                "timeout_s": 15.0,
            }
        try:
            reply = await self.pool.request(
                site_id,
                "skill_exec",
                timeout=max(timeout_s + 30.0, 90.0),
                **req,
            )
        except RunnerRequestError as e:
            raise RegistryError(_RUNNER_STATUS.get(e.code, 502), e.code, e.message) from e
        result = reply.get("result") if isinstance(reply, dict) else None
        logged_in = False
        pending = False
        if isinstance(reply, dict) and reply.get("logged_in") is True:
            logged_in = True
        if isinstance(reply, dict) and reply.get("pending") is True:
            pending = True
        if isinstance(result, dict):
            if device_qr:
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                logged_in = data.get("logged_in") is True
            elif oauth_link:
                code = result.get("code")
                if code == success_code:
                    logged_in = True
                elif isinstance(code, int) and code in pending_codes:
                    pending = True
                if logical == "status" and code == success_code:
                    logged_in = True

        auth_url = reply.get("auth_url") if isinstance(reply, dict) else None
        out_job_id = reply.get("oauth_job_id") if isinstance(reply, dict) else None
        if not auth_url and isinstance(result, dict) and isinstance(result.get("msg"), str):
            m = re.search(r"https://[^\s\"'<>）\]]+", result["msg"])
            if m:
                auth_url = m.group(0)

        if capture_login and out_job_id:
            _OAUTH_JOB_SITES[str(out_job_id)] = (
                site_id,
                time.monotonic() + max(timeout_s, 60.0) + 60.0,
            )
        if browser_capture and logical == "status" and job_val and (logged_in or not pending):
            _OAUTH_JOB_SITES.pop(job_val, None)

        session_written = False
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if logical in {"login", "status", "refresh", "relogin"} and logged_in:
                await self._upsert_vault(s, server, "SKILL_SESSION", "1")
                session_written = True
            if logical == "logout":
                row = (
                    await s.execute(
                        select(VaultEntry).where(
                            VaultEntry.server_id == server_id, VaultEntry.name == "SKILL_SESSION"
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await s.delete(row)
                    server.vault_version = int(server.vault_version or 0) + 1
            # Skip noisy pending status polls (Connect polls every ~2s). Keep login start,
            # terminal success/failure, logout, and non-status actions.
            write_audit = not (logical == "status" and pending and not logged_in)
            if write_audit:
                s.add(
                    self._audit(
                        actor,
                        "skill.auth",
                        server_id,
                        {
                            "action": logical,
                            "cli_action": cli_action,
                            "logged_in": logged_in,
                            "pending": pending,
                            "site": site_id,
                            "has_token": bool(token_val),
                        },
                    )
                )
            await s.commit()
            names = await self._env_names(s, server_id)

        if session_written:
            await self.clear_error_and_reprobe(server_id, actor)

        return {
            "action": logical,
            "cli_action": cli_action,
            "exit_code": reply.get("exit_code"),
            "result": result,
            "stderr": reply.get("stderr"),
            "qr_png_base64": reply.get("qr_png_base64"),
            "auth_url": auth_url,
            "job_id": out_job_id or job_val or None,
            "pending": pending,
            "logged_in": logged_in,
            "has_skill_session": "SKILL_SESSION" in names,
        }

    async def _probe(self, server_id: uuid.UUID, actor: str) -> dict[str, Any]:
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            kind = server.kind
            command, args = server.command, list(server.args or [])
            preferred_regions = list(server.preferred_regions or [])
            if kind == "stdio":
                self._validate_argv(command, args)  # policy may have changed since create
                env = await self._decrypt_env(s, server_id)
                env = await self._inject_skill_bridge_manifest(s, server.slug, env)
            elif kind == "remote":
                if not server.url:
                    raise RegistryError(422, "invalid_url", "remote server needs a url")
                env = {}
            else:
                raise RegistryError(400, "unsupported", f"server kind {kind!r} cannot be probed")

        audit_extra: dict[str, Any] = {}
        if kind == "stdio":
            assert command is not None
            timeout_s = self.probe_timeout_s
            try:
                decision = self.pool.pick(preferred_regions)
                if decision is None:
                    raise RunnerRequestError("runner_unavailable", "runner not connected", sent=False)
                reply = await self.pool.request(
                    decision.site_id,
                    "probe",
                    timeout=timeout_s + PROBE_TRANSPORT_SLACK_S,
                    probe_id=uuid.uuid4().hex,
                    server_id=str(server_id),
                    argv=[command, *args],
                    trailing_allowlist=[],
                    env=env,
                    timeout_s=timeout_s,
                )
            except RunnerRequestError as e:
                raise RegistryError(_RUNNER_STATUS.get(e.code, 502), e.code, e.message) from e
            incoming = _normalize_tools(reply.get("tools"))
            audit_extra = {
                "site_id": decision.site_id,
                "preferred_miss": decision.preferred_miss,
            }
        else:
            if self.remote is None:
                raise RegistryError(503, "unsupported", "remote proxy is not configured")
            try:
                raw_tools = await self.remote.probe(server_id)
            except PortunusError as e:
                raise RegistryError(_RUNNER_STATUS.get(e.code, 502), e.code, e.message) from e
            incoming = _normalize_tools(raw_tools)

        return await self._commit_probe(server_id, actor, incoming, audit_extra)

    async def set_bearer_token(self, server_id: uuid.UUID, token: str, actor: str) -> dict[str, Any]:
        """Paste-token: store ``Authorization`` in the vault (step-up required at the API)."""
        if not isinstance(token, str) or not token.strip() or "\x00" in token:
            raise RegistryError(422, "invalid_request", "token required")
        token = token.strip()
        value = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if server.kind != "remote":
                raise RegistryError(400, "unsupported", "only remote servers take a bearer token")
            await self._upsert_vault(s, server, AUTH_HEADER_NAME, value)
            s.add(self._audit(actor, "server.auth_token", server_id, None))
            await s.commit()
            names = await self._env_names(s, server_id)
        await self.clear_error_and_reprobe(server_id, actor)
        server = await self._load_server(server_id)
        return await self._enriched_view(server, names)

    async def store_authorization_header(
        self, server_id: uuid.UUID, authorization: str, actor: str, *, action: str = "server.oauth"
    ) -> dict[str, Any]:
        """Persist a full Authorization header value after OAuth exchange."""
        if not isinstance(authorization, str) or not authorization.strip() or "\x00" in authorization:
            raise RegistryError(422, "invalid_oauth", "missing access token")
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if server.kind != "remote":
                raise RegistryError(400, "unsupported", "only remote servers support oauth")
            await self._upsert_vault(s, server, AUTH_HEADER_NAME, authorization.strip())
            s.add(self._audit(actor, action, server_id, None))
            await s.commit()
            names = await self._env_names(s, server_id)
        await self.clear_error_and_reprobe(server_id, actor)
        server = await self._load_server(server_id)
        return await self._enriched_view(server, names)

    async def read_oauth_client_secret(self, server_id: uuid.UUID) -> str | None:
        async with self._sf() as s:
            await self._get_server(s, server_id)
            row = (
                await s.execute(
                    select(VaultEntry).where(
                        VaultEntry.server_id == server_id,
                        VaultEntry.name == OAUTH_CLIENT_SECRET_NAME,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                return self.vault.open(
                    bytes(row.ciphertext),
                    vault_entry_id=str(row.id),
                    key_version=row.key_version,
                ).decode()
            except (VaultError, UnicodeDecodeError):
                raise RegistryError(500, "vault_error", "cannot decrypt oauth client secret") from None

    async def remote_oauth_config(self, server_id: uuid.UUID) -> dict[str, Any]:
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if server.kind != "remote":
                raise RegistryError(400, "unsupported", "only remote servers support oauth")
            try:
                cfg = validate_oauth_config(server.oauth)
            except OAuthError as e:
                raise RegistryError(e.status, e.code, e.message) from e
            if cfg is None:
                raise RegistryError(422, "invalid_oauth", "server has no oauth config")
            return cfg

    async def _commit_probe(
        self,
        server_id: uuid.UUID,
        actor: str,
        incoming: list[dict[str, Any]],
        audit_extra: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        async with self._sf() as s:
            server = await self._get_server(s, server_id)  # deleted while probing -> 404
            existing = {
                t.upstream_name: t
                for t in (await s.execute(select(Tool).where(Tool.server_id == server_id)))
                .scalars()
                .all()
            }
            added = 0
            changed: list[str] = []
            for item in incoming:
                digest = schema_hash(item["schema"])
                tool = existing.get(item["name"])
                if tool is None:
                    s.add(
                        Tool(
                            server_id=server_id,
                            upstream_name=item["name"],
                            description=item["description"],
                            schema=item["schema"],
                            schema_hash=digest,
                            enabled=True,
                        )
                    )
                    added += 1
                elif tool.schema_hash != digest or tool.description != item["description"]:
                    tool.description = item["description"]
                    tool.schema = item["schema"]
                    tool.schema_hash = digest
                    tool.enabled = True
                    changed.append(tool.upstream_name)
                elif not tool.enabled:
                    tool.enabled = True
                    changed.append(tool.upstream_name)
            seen = {i["name"] for i in incoming}
            missing = sorted(n for n in existing if n not in seen)
            server.last_probe_at = now
            server.last_ok_at = now
            s.add(
                self._audit(
                    actor,
                    "server.probe",
                    server_id,
                    {
                        "tools": len(incoming),
                        "added": added,
                        "changed": changed,
                        "missing": missing,
                        **audit_extra,
                    },
                )
            )
            await s.commit()
            tools = (
                (
                    await s.execute(
                        select(Tool).where(Tool.server_id == server_id).order_by(Tool.upstream_name)
                    )
                )
                .scalars()
                .all()
            )
            views = [tool_view(t) for t in tools]
        return {
            "server_id": str(server_id),
            "added": added,
            "changed": sorted(changed),
            "missing": missing,
            "tools": views,
        }

    # ---- helpers -----------------------------------------------------------------

    def _seal_entry(self, server_id: uuid.UUID, name: str, value: str) -> VaultEntry:
        entry_id = uuid.uuid4()
        version = self.vault.current_version
        blob = self.vault.seal(value.encode(), vault_entry_id=str(entry_id), key_version=version)
        return VaultEntry(
            id=entry_id,
            server_id=server_id,
            name=name,
            ciphertext=blob,
            key_version=version,
        )

    async def _upsert_vault(
        self, s: AsyncSession, server: Server, name: str, value: str
    ) -> None:
        row = (
            await s.execute(
                select(VaultEntry).where(
                    VaultEntry.server_id == server.id, VaultEntry.name == name
                )
            )
        ).scalar_one_or_none()
        version = self.vault.current_version
        if row is None:
            s.add(self._seal_entry(server.id, name, value))
        else:
            row.ciphertext = self.vault.seal(
                value.encode(), vault_entry_id=str(row.id), key_version=version
            )
            row.key_version = version
        server.vault_version = int(server.vault_version or 0) + 1

    @staticmethod
    async def _get_server(s: AsyncSession, server_id: uuid.UUID) -> Server:
        server = await s.get(Server, server_id)
        if server is None:
            raise RegistryError(404, "not_found", "server not found")
        return server

    async def _load_server(self, server_id: uuid.UUID) -> Server:
        async with self._sf() as s:
            return await self._get_server(s, server_id)

    @staticmethod
    async def _env_names(s: AsyncSession, server_id: uuid.UUID) -> list[str]:
        rows = await s.execute(select(VaultEntry.name).where(VaultEntry.server_id == server_id))
        return [n for (n,) in rows]

    async def _decrypt_env(self, s: AsyncSession, server_id: uuid.UUID) -> dict[str, str]:
        rows = (
            (await s.execute(select(VaultEntry).where(VaultEntry.server_id == server_id)))
            .scalars()
            .all()
        )
        env: dict[str, str] = {}
        try:
            for row in rows:
                plain = self.vault.open(
                    bytes(row.ciphertext), vault_entry_id=str(row.id), key_version=row.key_version
                )
                env[row.name] = plain.decode()
        except (VaultError, UnicodeDecodeError):
            # Messages from the vault never carry material, but don't forward them anyway.
            raise RegistryError(500, "vault_error", "cannot decrypt server env") from None
        return env

    @staticmethod
    def _audit(actor: str, action: str, target: uuid.UUID, detail: dict | None) -> AuditEvent:
        return AuditEvent(actor=actor, action=action, target=str(target), detail=detail)
