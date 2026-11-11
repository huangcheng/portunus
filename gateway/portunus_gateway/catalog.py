"""Apps catalog: well-known connect recipes stored in SQLite (no SPA rebuild to add apps)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import portunus_skill_bridge
from portunus_shared import SKILL_BRIDGE_MANIFEST_ENV
from portunus_skill_bridge.manifest import load_manifest

from .db.models import CATALOG_CONNECTIONS, CatalogApp, Server
from .registry import RegistryError

WEBDAV_MCP_PACKAGE = "webdav-mcp-server@1.0.4"
WEBDAV_MCP_ARGS = ["-y", "--ignore-scripts", WEBDAV_MCP_PACKAGE]

# Stdio spawn for DB-backed skill manifests (injected as PORTUNUS_MANIFEST at start).
SKILL_BRIDGE_COMMAND = "python"
SKILL_BRIDGE_ARGS = ["-m", "portunus_skill_bridge", "--manifest-env", SKILL_BRIDGE_MANIFEST_ENV]


def _skill_bridge_root() -> Path:
    return Path(portunus_skill_bridge.__file__).resolve().parent.parent


def _load_packaged_manifest(name: str) -> dict[str, Any]:
    path = _skill_bridge_root() / "manifests" / name
    return load_manifest(path).model_dump(mode="json", exclude_none=True)


# Seeded WeRead http_rpc manifest (operator Connect supplies WEREAD_API_KEY → vault).
WEREAD_MANIFEST: dict[str, Any] = _load_packaged_manifest("weread.example.yaml")
# 光鸭云盘 json_cli — skill tree shipped under third_party/; runners mount/copy it.
GUANGYAPAN_MANIFEST: dict[str, Any] = _load_packaged_manifest("guangyapan.example.yaml")
# Default paths on Portunus runners (see deploy). Overridable via Connect env.
GUANGYAPAN_SKILL_ROOT = "/var/lib/portunus/skills/guangyapan-drive"
GUANGYAPAN_STATE_DIR = "/var/lib/portunus/skill-state/guangyapan"
# 夸克网盘 json_cli — oauth_link (browser_capture + optional paste code).
QUARK_MANIFEST: dict[str, Any] = _load_packaged_manifest("quark.example.yaml")
QUARK_SKILL_ROOT = "/var/lib/portunus/skills/quarkclouddrive"
QUARK_STATE_DIR = "/var/lib/portunus/skill-state/quark"

# Seeded once when the table is empty. Operators can edit/delete afterward.
# ``categories`` drive browse filters (Composio-style); ``tags`` are freeform chips.
DEFAULT_APPS: list[dict[str, Any]] = [
    {
        "slug": "ardot",
        "label": "Ardot",
        "name": "Ardot",
        "description": "Tencent Design Ardot — read/write design files via remote MCP.",
        "connection": "oauth",
        "popular": True,
        "categories": ["Design"],
        "tags": ["oauth", "mcp", "design"],
        "sort_order": 10,
        "spec": {
            "url": "https://ardot.tencent.com/mcp",
            "authorize_url": "https://ardot.tencent.com/oauth/authorize",
            "token_url": "https://ardot.tencent.com/oauth/token",
            "scopes": ["mcp:use"],
            "resource": "https://ardot.tencent.com/mcp",
            "registration_endpoint": "https://ardot.tencent.com/oauth/register",
            "token_endpoint_auth_method": "none",
        },
    },
    {
        "slug": "dropbox",
        "label": "Dropbox",
        "name": "Dropbox",
        "description": "Files, sharing, and file requests via Dropbox MCP.",
        "connection": "oauth",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["oauth", "files", "storage"],
        "sort_order": 20,
        "spec": {
            "url": "https://mcp.dropbox.com/mcp",
            "authorize_url": "https://www.dropbox.com/oauth2/authorize",
            "token_url": "https://api.dropboxapi.com/oauth2/token",
            "scopes": [
                "account_info.read",
                "files.metadata.read",
                "files.content.read",
                "files.content.write",
                "sharing.read",
                "sharing.write",
                "file_requests.read",
                "file_requests.write",
            ],
        },
    },
    {
        "slug": "nutstore",
        "label": "Nutstore",
        "name": "Nutstore",
        "description": "坚果云 files over WebDAV (app password).",
        "connection": "webdav",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["webdav", "files", "storage"],
        "sort_order": 30,
        "spec": {
            "root_url": "https://dav.jianguoyun.com/dav/",
            "root_path": "/",
            "package": WEBDAV_MCP_PACKAGE,
            "args": WEBDAV_MCP_ARGS,
        },
    },
    {
        "slug": "koofr",
        "label": "Koofr",
        "name": "Koofr",
        "description": "Koofr cloud storage over WebDAV (app password).",
        "connection": "webdav",
        "popular": False,
        "categories": ["Storage"],
        "tags": ["webdav", "files", "storage"],
        "sort_order": 40,
        "spec": {
            "root_url": "https://app.koofr.net/dav/Koofr",
            "root_path": "/",
            "package": WEBDAV_MCP_PACKAGE,
            "args": WEBDAV_MCP_ARGS,
        },
    },
    {
        "slug": "flomo",
        "label": "flomo",
        "name": "flomo",
        "description": "flomo memos via remote MCP.",
        "connection": "http",
        "popular": False,
        "categories": ["Productivity"],
        "tags": ["notes", "memos", "http"],
        "sort_order": 50,
        "spec": {"url": "https://flomoapp.com/mcp"},
    },
    {
        "slug": "dida365",
        "label": "滴答清单",
        "name": "滴答清单",
        "description": "TickTick / 滴答清单 tasks via remote MCP.",
        "connection": "http",
        "popular": False,
        "categories": ["Productivity"],
        "tags": ["tasks", "todo", "http"],
        "sort_order": 60,
        "spec": {"url": "https://mcp.dida365.com"},
    },
    {
        "slug": "weiyun",
        "label": "微云",
        "name": "微云",
        "description": "Tencent Weiyun drive via remote MCP (WyHeader token).",
        "connection": "http",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["weiyun", "tencent", "drive", "http"],
        "sort_order": 55,
        "spec": {
            "url": "https://www.weiyun.com/api/v3/mcpserver",
            "auth_header": "WyHeader",
            "token_prefix": "mcp_token=",
        },
    },
    {
        "slug": "weread",
        "label": "微信读书",
        "name": "微信读书",
        "description": "WeRead search, shelf, and notes via skill-bridge (API key).",
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Reading"],
        "tags": ["weread", "books", "http_rpc"],
        "sort_order": 70,
        "spec": {
            "manifest": WEREAD_MANIFEST,
            "secrets": [
                {"name": "WEREAD_API_KEY", "label": "WeRead API key", "secret": True},
            ],
        },
    },
    {
        "slug": "guangyapan",
        "label": "光鸭云盘",
        "name": "光鸭云盘",
        "description": "Guangyapan Drive via skill-bridge (device_qr login; agents get drive tools only).",
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["guangyapan", "drive", "json_cli"],
        "sort_order": 75,
        "spec": {
            "manifest": GUANGYAPAN_MANIFEST,
            "secrets": [],
            "env_defaults": {
                "SKILL_ROOT": GUANGYAPAN_SKILL_ROOT,
                "STATE_DIR": GUANGYAPAN_STATE_DIR,
            },
        },
    },
    {
        "slug": "quark",
        "label": "夸克网盘",
        "name": "夸克网盘",
        "description": "Quark Drive via skill-bridge (oauth_link + browser_capture; agents get drive tools only).",
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["quark", "drive", "json_cli"],
        "sort_order": 76,
        "spec": {
            "manifest": QUARK_MANIFEST,
            "secrets": [],
            "env_defaults": {
                "SKILL_ROOT": QUARK_SKILL_ROOT,
                "STATE_DIR": QUARK_STATE_DIR,
            },
        },
    },
    {
        "slug": "pikpak",
        "label": "PikPak",
        "name": "PikPak",
        "description": "PikPak cloud drive via official CLI MCP (pikpak mcp --stdio + PAT).",
        "connection": "stdio",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["pikpak", "drive", "mcp", "stdio"],
        "sort_order": 78,
        "spec": {
            "command": "pikpak",
            "args": ["mcp", "--stdio"],
            "env": [
                {"name": "PIKPAK_TOKEN", "label": "Personal Access Token", "secret": True},
            ],
            "env_defaults": {
                "PIKPAK_DIR": "/var/lib/portunus/skill-state/pikpak",
                "PIKPAK_NO_UPDATE_NOTIFIER": "1",
            },
        },
    },
]


def _normalize_labels(raw: Any, *, field: str, limit: int = 16) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryError(422, "invalid_request", f"{field} must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RegistryError(422, "invalid_request", f"{field} must be a list of strings")
        label = item.strip()
        if len(label) > 40:
            raise RegistryError(422, "invalid_request", f"{field} entries must be ≤40 chars")
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) > limit:
            raise RegistryError(422, "invalid_request", f"{field} supports at most {limit} entries")
    return out


def catalog_view(row: CatalogApp) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "slug": row.slug,
        "label": row.label,
        "name": row.name,
        "description": row.description,
        "connection": row.connection,
        "popular": row.popular,
        "categories": list(row.categories or []),
        "tags": list(row.tags or []),
        "spec": dict(row.spec or {}),
        "sort_order": row.sort_order,
    }


def _require_str(spec: dict[str, Any], key: str) -> str:
    val = spec.get(key)
    if not isinstance(val, str) or not val.strip():
        raise RegistryError(422, "invalid_request", f"spec.{key} is required")
    return val.strip()


def validate_spec(connection: str, spec: Any) -> dict[str, Any]:
    if connection not in CATALOG_CONNECTIONS:
        raise RegistryError(400, "unsupported", f"connection {connection!r} is not supported")
    if not isinstance(spec, dict):
        raise RegistryError(422, "invalid_request", "spec must be an object")
    if connection == "oauth":
        out: dict[str, Any] = {
            "url": _require_str(spec, "url"),
            "authorize_url": _require_str(spec, "authorize_url"),
            "token_url": _require_str(spec, "token_url"),
        }
        scopes = spec.get("scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
            raise RegistryError(422, "invalid_request", "spec.scopes must be a list of strings")
        out["scopes"] = list(scopes)
        client_id = spec.get("client_id")
        if client_id is not None:
            if not isinstance(client_id, str) or not client_id.strip():
                raise RegistryError(422, "invalid_request", "spec.client_id must be a non-empty string")
            out["client_id"] = client_id.strip()
        for opt in ("resource", "registration_endpoint"):
            raw = spec.get(opt)
            if raw is None or raw == "":
                continue
            if not isinstance(raw, str) or not raw.startswith("https://"):
                raise RegistryError(422, "invalid_request", f"spec.{opt} must be an https URL")
            out[opt] = raw.strip()
        method = spec.get("token_endpoint_auth_method")
        if method is not None:
            if method not in ("none", "client_secret_basic", "client_secret_post"):
                raise RegistryError(422, "invalid_request", "invalid token_endpoint_auth_method")
            out["token_endpoint_auth_method"] = method
        return out
    if connection == "http":
        out = {"url": _require_str(spec, "url")}
        # Optional custom request header for Connect (e.g. Weiyun WyHeader).
        auth_header = spec.get("auth_header")
        if auth_header is not None and auth_header != "":
            if not isinstance(auth_header, str) or not auth_header.strip():
                raise RegistryError(422, "invalid_request", "spec.auth_header must be a non-empty string")
            out["auth_header"] = auth_header.strip()
            prefix = spec.get("token_prefix")
            if prefix is None or prefix == "":
                out["token_prefix"] = ""
            elif not isinstance(prefix, str):
                raise RegistryError(422, "invalid_request", "spec.token_prefix must be a string")
            else:
                out["token_prefix"] = prefix
        return out
    if connection == "webdav":
        out = {
            "root_url": _require_str(spec, "root_url"),
            "root_path": str(spec.get("root_path") or "/").strip() or "/",
            "package": str(spec.get("package") or WEBDAV_MCP_PACKAGE),
            "args": list(spec.get("args") or WEBDAV_MCP_ARGS),
        }
        if not isinstance(out["args"], list) or not all(isinstance(a, str) for a in out["args"]):
            raise RegistryError(422, "invalid_request", "spec.args must be a string list")
        return out
    if connection == "skill_bridge":
        return _validate_skill_bridge_spec(spec)
    # stdio
    command = _require_str(spec, "command")
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise RegistryError(422, "invalid_request", "spec.args must be a string list")
    env = spec.get("env", [])
    if not isinstance(env, list):
        raise RegistryError(422, "invalid_request", "spec.env must be a list")
    env_out: list[dict[str, Any]] = []
    for item in env:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise RegistryError(422, "invalid_request", "spec.env entries need a name")
        env_out.append(
            {
                "name": item["name"].strip(),
                "label": str(item.get("label") or item["name"]),
                "secret": bool(item.get("secret", True)),
            }
        )
    out: dict[str, Any] = {"command": command, "args": list(args), "env": env_out}
    defaults = spec.get("env_defaults") or {}
    if defaults:
        if not isinstance(defaults, dict):
            raise RegistryError(422, "invalid_request", "spec.env_defaults must be an object")
        env_defaults: dict[str, str] = {}
        for k, v in defaults.items():
            if not isinstance(k, str) or not k.strip() or not isinstance(v, str):
                raise RegistryError(
                    422, "invalid_request", "spec.env_defaults must map strings to strings"
                )
            env_defaults[k.strip()] = v
        out["env_defaults"] = env_defaults
    return out


def _validate_skill_bridge_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """DB-backed skill manifest + secret field defs for Connect."""
    from portunus_skill_bridge.manifest import Manifest

    raw_manifest = spec.get("manifest")
    if not isinstance(raw_manifest, dict):
        raise RegistryError(422, "invalid_request", "spec.manifest must be an object")
    try:
        manifest = Manifest.model_validate(raw_manifest)
    except Exception as e:
        raise RegistryError(422, "invalid_request", f"invalid skill manifest: {e}") from e
    secrets = spec.get("secrets", [])
    if not isinstance(secrets, list):
        raise RegistryError(422, "invalid_request", "spec.secrets must be a list")
    secrets_out: list[dict[str, Any]] = []
    for item in secrets:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise RegistryError(422, "invalid_request", "spec.secrets entries need a name")
        secrets_out.append(
            {
                "name": item["name"].strip(),
                "label": str(item.get("label") or item["name"]),
                "secret": bool(item.get("secret", True)),
            }
        )
    # Auth env from manifest must be listed in secrets when bearer/header auth is used.
    if manifest.auth.type in ("bearer_env", "header_env") and manifest.auth.env:
        names = {s["name"] for s in secrets_out}
        if manifest.auth.env not in names:
            raise RegistryError(
                422,
                "invalid_request",
                f"spec.secrets must include auth env {manifest.auth.env!r}",
            )
    env_defaults = spec.get("env_defaults") or {}
    if not isinstance(env_defaults, dict):
        raise RegistryError(422, "invalid_request", "spec.env_defaults must be an object")
    env_out: dict[str, str] = {}
    for k, v in env_defaults.items():
        if not isinstance(k, str) or not k.strip() or not isinstance(v, str):
            raise RegistryError(
                422, "invalid_request", "spec.env_defaults must map strings to strings"
            )
        env_out[k.strip()] = v
    return {
        "manifest": manifest.model_dump(mode="json"),
        "secrets": secrets_out,
        "env_defaults": env_out,
        "command": str(spec.get("command") or SKILL_BRIDGE_COMMAND),
        "args": list(spec.get("args") or SKILL_BRIDGE_ARGS),
    }


class CatalogStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def ensure_defaults(self) -> int:
        """Insert DEFAULT_APPS when empty; backfill categories/tags on known seed slugs."""
        inserted = 0
        async with self._sf() as s:
            existing = (await s.execute(select(CatalogApp.id).limit(1))).first()
            if not existing:
                for i, raw in enumerate(DEFAULT_APPS):
                    spec = validate_spec(raw["connection"], raw["spec"])
                    s.add(
                        CatalogApp(
                            id=uuid.uuid4(),
                            slug=raw["slug"],
                            label=raw["label"],
                            name=raw["name"],
                            description=raw["description"],
                            connection=raw["connection"],
                            popular=bool(raw.get("popular")),
                            categories=_normalize_labels(raw.get("categories"), field="categories"),
                            tags=_normalize_labels(raw.get("tags"), field="tags"),
                            spec=spec,
                            sort_order=int(raw.get("sort_order", (i + 1) * 10)),
                        )
                    )
                await s.commit()
                inserted = len(DEFAULT_APPS)

        # Idempotent taxonomy sync for deploys that already have the seed rows.
        by_slug = {a["slug"]: a for a in DEFAULT_APPS}
        async with self._sf() as s:
            rows = (await s.execute(select(CatalogApp))).scalars().all()
            dirty = False
            for row in rows:
                seed = by_slug.get(row.slug)
                if not seed:
                    continue
                if not list(row.categories or []) and seed.get("categories"):
                    row.categories = _normalize_labels(seed.get("categories"), field="categories")
                    dirty = True
                if not list(row.tags or []) and seed.get("tags"):
                    row.tags = _normalize_labels(seed.get("tags"), field="tags")
                    dirty = True
            if dirty:
                await s.commit()

        # Idempotent: add new skill_bridge seeds (e.g. weread) on existing deploys,
        # and refresh skill_bridge manifests when the packaged seed grows (tool list).
        added = 0
        refreshed = 0
        async with self._sf() as s:
            for raw in DEFAULT_APPS:
                if raw.get("connection") != "skill_bridge":
                    continue
                spec = validate_spec(raw["connection"], raw["spec"])
                row = (
                    await s.execute(select(CatalogApp).where(CatalogApp.slug == raw["slug"]))
                ).scalar_one_or_none()
                if row is None:
                    s.add(
                        CatalogApp(
                            id=uuid.uuid4(),
                            slug=raw["slug"],
                            label=raw["label"],
                            name=raw["name"],
                            description=raw["description"],
                            connection=raw["connection"],
                            popular=bool(raw.get("popular")),
                            categories=_normalize_labels(raw.get("categories"), field="categories"),
                            tags=_normalize_labels(raw.get("tags"), field="tags"),
                            spec=spec,
                            sort_order=int(raw.get("sort_order", 0)),
                        )
                    )
                    added += 1
                    continue
                seed_manifest = spec.get("manifest") or {}
                live_manifest = (row.spec or {}).get("manifest") or {}
                seed_tools = seed_manifest.get("tools") or []
                live_tools = live_manifest.get("tools") or []
                seed_names = {t.get("name") for t in seed_tools if isinstance(t, dict)}
                live_names = {t.get("name") for t in live_tools if isinstance(t, dict)}
                seed_auth = seed_manifest.get("auth") if isinstance(seed_manifest, dict) else None
                live_auth = live_manifest.get("auth") if isinstance(live_manifest, dict) else None
                auth_changed = seed_auth != live_auth
                seed_defaults = spec.get("env_defaults") or {}
                live_defaults = (row.spec or {}).get("env_defaults") or {}
                defaults_changed = seed_defaults != live_defaults
                if (seed_names and seed_names != live_names) or auth_changed or defaults_changed:
                    row.spec = spec
                    linked = (
                        await s.execute(
                            select(Server).where(Server.slug == row.slug, Server.kind == "stdio")
                        )
                    ).scalars().all()
                    for srv in linked:
                        srv.vault_version = int(srv.vault_version or 0) + 1
                    refreshed += 1
            if added or refreshed:
                await s.commit()
        return inserted + added + refreshed

    async def list_apps(self) -> list[dict[str, Any]]:
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(CatalogApp).order_by(CatalogApp.sort_order, CatalogApp.slug)
                )
            ).scalars().all()
            return [catalog_view(r) for r in rows]

    async def get(self, app_id: uuid.UUID) -> dict[str, Any]:
        async with self._sf() as s:
            row = await s.get(CatalogApp, app_id)
            if row is None:
                raise RegistryError(404, "not_found", "catalog app not found")
            return catalog_view(row)

    async def create(
        self,
        *,
        slug: str,
        label: str,
        name: str,
        description: str,
        connection: str,
        spec: dict[str, Any],
        popular: bool = False,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        sort_order: int = 100,
        actor: str,
    ) -> dict[str, Any]:
        spec_n = validate_spec(connection, spec)
        row = CatalogApp(
            id=uuid.uuid4(),
            slug=slug.casefold(),
            label=label,
            name=name,
            description=description,
            connection=connection,
            popular=popular,
            categories=_normalize_labels(categories, field="categories"),
            tags=_normalize_labels(tags, field="tags"),
            spec=spec_n,
            sort_order=sort_order,
        )
        async with self._sf() as s:
            try:
                s.add(row)
                await s.commit()
                await s.refresh(row)
            except IntegrityError:
                await s.rollback()
                raise RegistryError(409, "conflict", "slug already in use") from None
        return catalog_view(row)

    async def update(
        self,
        app_id: uuid.UUID,
        *,
        actor: str,
        label: str | None = None,
        name: str | None = None,
        description: str | None = None,
        popular: bool | None = None,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        sort_order: int | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._sf() as s:
            row = await s.get(CatalogApp, app_id)
            if row is None:
                raise RegistryError(404, "not_found", "catalog app not found")
            if label is not None:
                row.label = label
            if name is not None:
                row.name = name
            if description is not None:
                row.description = description
            if popular is not None:
                row.popular = popular
            if categories is not None:
                row.categories = _normalize_labels(categories, field="categories")
            if tags is not None:
                row.tags = _normalize_labels(tags, field="tags")
            if sort_order is not None:
                row.sort_order = sort_order
            if spec is not None:
                row.spec = validate_spec(row.connection, spec)
            # Hot update: bump linked stdio servers so reconcile respawns skill-bridge.
            if row.connection == "skill_bridge" and spec is not None:
                linked = (
                    await s.execute(select(Server).where(Server.slug == row.slug, Server.kind == "stdio"))
                ).scalars().all()
                for srv in linked:
                    srv.vault_version = int(srv.vault_version or 0) + 1
            await s.commit()
            await s.refresh(row)
            return catalog_view(row)

    async def delete(self, app_id: uuid.UUID, *, actor: str) -> None:
        async with self._sf() as s:
            row = await s.get(CatalogApp, app_id)
            if row is None:
                raise RegistryError(404, "not_found", "catalog app not found")
            await s.delete(row)
            await s.commit()
