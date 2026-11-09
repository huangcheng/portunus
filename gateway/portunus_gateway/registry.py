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
from .runner_client import RunnerRequestError
from .vault import Vault, VaultError

MAX_ENV_ENTRIES = 256  # mirrors the runner's bound
MAX_ENV_BYTES = 128 * 1024
MAX_TOOLS = 2000
PROBE_TRANSPORT_SLACK_S = 5  # the runner enforces timeout_s; leave room for its reply
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_FORBIDDEN_ENV_PREFIXES = ("PORTUNUS_", "LD_", "DYLD_")  # runner rejects these too
_RUNNER_STATUS = {
    "runner_unavailable": 503,
    "upstream_timeout": 504,
    "spawn_rejected": 422,
    "spawn_failed": 502,
    "upstream_crash": 502,
    "payload_too_large": 502,
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


def server_view(server: Server, env_names: list[str]) -> dict[str, Any]:
    return {
        "id": str(server.id),
        "name": server.name,
        "slug": server.slug,
        "kind": server.kind,
        "enabled": server.enabled,
        "command": server.command,
        "args": list(server.args or []),
        "url": server.url,
        "vault_version": server.vault_version,
        "env_names": sorted(env_names),  # names only, never values
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
        probe_timeout_s: int = DEFAULTS.probe_timeout_s,
        max_probes: int = DEFAULTS.max_probes,
    ) -> None:
        self._sf = session_factory
        self.vault = vault
        self.runner = runner
        self.trusted_path = list(trusted_path)
        self.binary_allowlist = list(binary_allowlist)
        self.slots = slots  # SlotManager (disable_server) once wired by the app
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
    ) -> dict[str, Any]:
        if kind != "stdio":
            raise RegistryError(400, "unsupported", f"server kind {kind!r} is not supported yet")
        self._validate_argv(command, args)
        env = validate_env(env)
        server = Server(
            id=uuid.uuid4(),
            name=name,
            slug=slug,
            kind="stdio",
            enabled=False,
            command=command,
            args=list(args),
            vault_version=1 if env else 0,
        )
        entries = []
        for key, value in env.items():
            entry_id = uuid.uuid4()
            version = self.vault.current_version
            blob = self.vault.seal(
                value.encode(), vault_entry_id=str(entry_id), key_version=version
            )
            entries.append(
                VaultEntry(
                    id=entry_id,
                    server_id=server.id,
                    name=key,
                    ciphertext=blob,
                    key_version=version,
                )
            )
        async with self._sf() as s:
            try:
                s.add(server)
                await s.flush()  # server row first: vault entries reference it
                s.add_all(entries)
                s.add(self._audit(actor, "server.create", server.id, {"slug": server.slug}))
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise RegistryError(409, "conflict", "slug already in use") from None
        return server_view(server, list(env))

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
        assert command is not None
        return {
            "argv": [command, *args],
            "trailing_allowlist": [],
            "env": env,
            "vault_version": vault_version,
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
            if server.kind != "stdio":
                raise RegistryError(400, "unsupported", "only stdio servers can be probed")
            command, args = server.command, list(server.args or [])
            self._validate_argv(command, args)  # policy may have changed since create
            env = await self._decrypt_env(s, server_id)
        assert command is not None

        timeout_s = self.probe_timeout_s
        try:
            reply = await self.runner.request(
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
                            enabled=False,  # new tools are never enabled by a probe
                        )
                    )
                    added += 1
                elif tool.schema_hash != digest or tool.description != item["description"]:
                    tool.description = item["description"]
                    tool.schema = item["schema"]
                    tool.schema_hash = digest
                    tool.enabled = False  # drift: operator must re-approve
                    changed.append(tool.upstream_name)
            seen = {i["name"] for i in incoming}
            missing = sorted(n for n in existing if n not in seen)
            s.add(
                self._audit(
                    actor,
                    "server.probe",
                    server_id,
                    {"tools": len(incoming), "added": added, "changed": changed, "missing": missing},
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
