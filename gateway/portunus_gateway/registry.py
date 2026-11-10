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

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from portunus_shared import DEFAULTS, PortunusError, validate_stdio_command
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditEvent, Server, Tool, VaultEntry
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
        if k.upper().startswith(_FORBIDDEN_ENV_PREFIXES):
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


def server_view(server: Server, env_names: list[str]) -> dict[str, Any]:
    # Hide reserved vault names from the operator UI.
    public_names = sorted(n for n in env_names if not n.lower().startswith("portunus."))
    return {
        "id": str(server.id),
        "name": server.name,
        "slug": server.slug,
        "kind": server.kind,
        "enabled": server.enabled,
        "command": server.command,
        "args": list(server.args or []),
        "url": server.url,
        "oauth": oauth_public_view(server.oauth if isinstance(server.oauth, dict) else None),
        "has_auth": any(n.lower() == AUTH_HEADER_NAME.lower() for n in env_names),
        "preferred_regions": list(server.preferred_regions or []),
        "vault_version": server.vault_version,
        "env_names": public_names,  # names only, never values
        "created_at": server.created_at.isoformat() if server.created_at else None,
    }


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

    @property
    def session_factory(self) -> Callable[[], AsyncSession]:
        return self._sf

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
        return server_view(server, list(secrets))

    async def update_server(
        self,
        server_id: uuid.UUID,
        *,
        actor: str,
        preferred_regions: list[str] | None = None,
        oauth: dict[str, Any] | None = None,
        oauth_client_secret: str | None = None,
    ) -> dict[str, Any]:
        if preferred_regions is None and oauth is None and oauth_client_secret is None:
            raise RegistryError(422, "invalid_request", "no fields to update")
        regions = validate_regions(preferred_regions) if preferred_regions is not None else None
        try:
            oauth_cfg = validate_oauth_config(oauth) if oauth is not None else None
        except OAuthError as e:
            raise RegistryError(e.status, e.code, e.message) from e
        if oauth_client_secret is not None:
            if not isinstance(oauth_client_secret, str) or "\x00" in oauth_client_secret:
                raise RegistryError(422, "invalid_oauth", "invalid oauth_client_secret")
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            if (oauth is not None or oauth_client_secret is not None) and server.kind != "remote":
                raise RegistryError(422, "invalid_request", "stdio servers do not take oauth")
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
            s.add(self._audit(actor, "server.update", server_id, audit or None))
            await s.commit()
            names = await self._env_names(s, server_id)
            return server_view(server, names)

    async def list_servers(self) -> list[dict[str, Any]]:
        async with self._sf() as s:
            servers = (await s.execute(select(Server).order_by(Server.created_at))).scalars().all()
            names: dict[uuid.UUID, list[str]] = {}
            rows = await s.execute(select(VaultEntry.server_id, VaultEntry.name))
            for sid, n in rows:
                names.setdefault(sid, []).append(n)
        return [server_view(sv, names.get(sv.id, [])) for sv in servers]

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
            view = server_view(server, names)
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
        finally:
            self._probing.discard(server_id)

    async def _probe(self, server_id: uuid.UUID, actor: str) -> dict[str, Any]:
        async with self._sf() as s:
            server = await self._get_server(s, server_id)
            kind = server.kind
            command, args = server.command, list(server.args or [])
            preferred_regions = list(server.preferred_regions or [])
            if kind == "stdio":
                self._validate_argv(command, args)  # policy may have changed since create
                env = await self._decrypt_env(s, server_id)
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
            return server_view(server, names)

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
            return server_view(server, names)

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
        async with self._sf() as s:
            await self._get_server(s, server_id)  # deleted while probing -> 404
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
