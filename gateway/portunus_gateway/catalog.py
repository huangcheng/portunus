"""Apps catalog: well-known connect recipes stored in SQLite (no SPA rebuild to add apps)."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import portunus_skill_bridge
from portunus_shared import SKILL_BRIDGE_MANIFEST_ENV
from portunus_skill_bridge.manifest import load_manifest

from .db.models import CATALOG_CONNECTIONS, CatalogApp, Server, Tool, VaultEntry
from .registry import RegistryError

log = logging.getLogger(__name__)

WEBDAV_MCP_PACKAGE = "webdav-mcp-server@1.0.4"
# Runner-local rclone connector shim (provisioned by deploy/ensure-state-dirs.sh
# siblings; see deploy notes). Absolute path so the closed argv grammar admits it
# via binary_allowlist. Bridges a pasted rclone remote config to webdav-mcp-server.
RCLONE_SHIM = "/var/lib/portunus/skills/rclone-shim/shim"
WEBDAV_MCP_ARGS = ["-y", "--ignore-scripts", WEBDAV_MCP_PACKAGE]

# Soft affinity for catalog Connect when the operator leaves preferred_regions empty.
# Portable tags only — each deploy labels runners via PORTUNUS_REGIONS
# (e.g. ``overseas`` / ``domestic``). SiteHealth still fails over on network errors.
EGRESS_ANY = "any"
EGRESS_OVERSEAS = "overseas"
EGRESS_DOMESTIC = "domestic"
EGRESS_CHOICES = frozenset({EGRESS_ANY, EGRESS_OVERSEAS, EGRESS_DOMESTIC})
OVERSEAS_PREFERRED_REGIONS = [EGRESS_OVERSEAS]
DOMESTIC_PREFERRED_REGIONS = [EGRESS_DOMESTIC]
# Legacy alias kept for tests / callers that still import the frozenset.
OVERSEAS_EGRESS_SLUGS = frozenset(
    {
        "yandex",
        "drime",
        "box",
        "onedrive",
        "gdrive",
        "mega",
        "raindrop",
        "github",
        "cloudflare",
        "dropbox",
        "pikpak",
        "koofr",
        "vultr",
        "vercel",
    }
)
DOMESTIC_EGRESS_SLUGS = frozenset(
    {
        "nutstore",
        "weiyun",
        "flomo",
        "dida365",
        "youdaonote",
        "weread",
        "cubox",
        "ima",
        "qqmusic",
        "guangyapan",
        "ardot",
        "aliyun",
        "tencentcloud",
        "flyai",
    }
)


def preferred_regions_for_egress(egress: str | None) -> list[str]:
    """Map catalog ``egress`` to server preferred_regions (soft affinity tags)."""
    e = (egress or EGRESS_ANY).strip().casefold()
    if e == EGRESS_OVERSEAS:
        return list(OVERSEAS_PREFERRED_REGIONS)
    if e == EGRESS_DOMESTIC:
        return list(DOMESTIC_PREFERRED_REGIONS)
    return []


def normalize_egress(raw: Any) -> str:
    if raw is None or raw == "":
        return EGRESS_ANY
    if not isinstance(raw, str) or raw.strip().casefold() not in EGRESS_CHOICES:
        raise RegistryError(
            422,
            "invalid_request",
            f"egress must be one of {sorted(EGRESS_CHOICES)}",
        )
    return raw.strip().casefold()


def egress_for_slug(slug: str) -> str:
    """Resolve catalog egress for a slug (loaded TOML → DEFAULT_APPS → legacy sets)."""
    key = (slug or "").casefold()
    if not key:
        return EGRESS_ANY
    try:
        from .toml_catalog import get_loaded

        loaded = get_loaded()
    except Exception:  # noqa: BLE001 — catalog optional at import time
        loaded = None
    if loaded is not None:
        for recipe in loaded.recipes:
            if recipe.slug.casefold() == key and recipe.enabled:
                return normalize_egress(getattr(recipe, "egress", EGRESS_ANY))
    for app in DEFAULT_APPS:
        if str(app.get("slug", "")).casefold() == key:
            if "egress" in app:
                return normalize_egress(app.get("egress"))
            break
    if key in OVERSEAS_EGRESS_SLUGS:
        return EGRESS_OVERSEAS
    if key in DOMESTIC_EGRESS_SLUGS:
        return EGRESS_DOMESTIC
    return EGRESS_ANY


def preferred_regions_for_slug(slug: str) -> list[str]:
    """Default preferred_regions for catalog/connect when the operator left them empty."""
    return preferred_regions_for_egress(egress_for_slug(slug))

# Stdio spawn for DB-backed skill manifests (injected as PORTUNUS_MANIFEST at start).
SKILL_BRIDGE_COMMAND = "python"
SKILL_BRIDGE_ARGS = ["-m", "portunus_skill_bridge", "--manifest-env", SKILL_BRIDGE_MANIFEST_ENV]


def _default_egress_for_slug(slug: str) -> str:
    key = (slug or "").casefold()
    if key in OVERSEAS_EGRESS_SLUGS:
        return EGRESS_OVERSEAS
    if key in DOMESTIC_EGRESS_SLUGS:
        return EGRESS_DOMESTIC
    return EGRESS_ANY


def _skill_bridge_root() -> Path:
    return Path(portunus_skill_bridge.__file__).resolve().parent.parent


def _load_packaged_manifest(name: str) -> dict[str, Any]:
    path = _skill_bridge_root() / "manifests" / name
    return load_manifest(path).model_dump(mode="json", exclude_none=True)


# Seeded WeRead http_rpc manifest (operator Connect supplies WEREAD_API_KEY → vault).
WEREAD_MANIFEST: dict[str, Any] = _load_packaged_manifest("weread.example.yaml")
# Tencent IMA OpenAPI — Client ID + API Key headers (notes + knowledge base).
IMA_MANIFEST: dict[str, Any] = _load_packaged_manifest("ima.example.yaml")
# QQ Music Agent Gateway — Bearer QQMUSIC_API_KEY; params+comm body envelope.
QQMUSIC_MANIFEST: dict[str, Any] = _load_packaged_manifest("qqmusic.example.yaml")
# Cubox json_cli — wraps official cubox-cli (fixed API Extension token).
CUBOX_MANIFEST: dict[str, Any] = _load_packaged_manifest("cubox.example.yaml")
# 飞猪 FlyAI json_cli — wraps @fly-ai/flyai-cli (Fliggy travel search).
FLYAI_MANIFEST: dict[str, Any] = _load_packaged_manifest("flyai.example.yaml")
# 光鸭云盘 json_cli — skill tree shipped under third_party/; runners mount/copy it.
GUANGYAPAN_MANIFEST: dict[str, Any] = _load_packaged_manifest("guangyapan.example.yaml")
# Default paths on Portunus runners (see deploy). Overridable via Connect env.
GUANGYAPAN_SKILL_ROOT = "/var/lib/portunus/skills/guangyapan-drive"
GUANGYAPAN_STATE_DIR = "/var/lib/portunus/skill-state/guangyapan"
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
        "slug": "tencentcloud",
        "label": "腾讯云",
        "name": "腾讯云",
        "description": (
            "Tencent Cloud CVM management MCP (instances, VPC, security groups, monitoring) "
            "via SecretId/SecretKey."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Developer Tools"],
        "tags": ["tencentcloud", "cvm", "stdio", "mcp"],
        "sort_order": 12,
        "spec": {
            "command": "uvx",
            "args": ["mcp-server-cvm==0.1.12"],
            "env": [
                {
                    "name": "TENCENTCLOUD_SECRET_ID",
                    "label": "SecretId",
                    "secret": True,
                },
                {
                    "name": "TENCENTCLOUD_SECRET_KEY",
                    "label": "SecretKey",
                    "secret": True,
                },
            ],
            "env_defaults": {"TENCENTCLOUD_REGION": "ap-guangzhou"},
            "setup_hints": [
                (
                    "Create API keys in "
                    "[CAM → API Keys](https://console.cloud.tencent.com/cam/capi). "
                    "Package: "
                    "[mcp-server-cvm]"
                    "(https://github.com/TencentCloudCommunity/mcp-server/tree/main/src/mcp-server-cvm). "
                    "Default region is ap-guangzhou."
                ),
            ],
        },
    },
    {
        "slug": "github",
        "label": "GitHub",
        "name": "GitHub",
        "description": (
            "GitHub remote MCP (api.githubcopilot.com). OAuth via your GitHub OAuth App "
            "or GitHub App — paste Client ID/secret at Connect (no DCR)."
        ),
        "connection": "oauth",
        "popular": True,
        "categories": ["Developer Tools"],
        "tags": ["github", "oauth", "mcp", "repos"],
        "sort_order": 15,
        "spec": {
            "url": "https://api.githubcopilot.com/mcp/",
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "token_endpoint_auth_method": "client_secret_post",
            "resource": "https://api.githubcopilot.com/mcp/",
            "scopes": [
                "repo",
                "read:org",
                "read:user",
                "user:email",
                "gist",
                "notifications",
                "read:packages",
                "read:project",
            ],
            "setup_hints": [
                (
                    "Create a GitHub [OAuth App](https://github.com/settings/developers) or "
                    "[GitHub App](https://docs.github.com/en/apps/creating-github-apps/"
                    "about-creating-github-apps/about-creating-github-apps) (no DCR). "
                    "Set the callback to the Redirect URI below. "
                    "[MCP setup docs](https://github.com/github/github-mcp-server)."
                ),
            ],
        },
    },
    {
        "slug": "cloudflare",
        "label": "Cloudflare",
        "name": "Cloudflare",
        "description": (
            "Cloudflare API MCP (Code Mode search/execute/docs) via OAuth. "
            "Maps to https://mcp.cloudflare.com/mcp from Cloudflare agent-setup."
        ),
        "connection": "oauth",
        "popular": True,
        "categories": ["Developer Tools"],
        "tags": ["cloudflare", "oauth", "mcp", "workers"],
        "sort_order": 16,
        "spec": {
            "url": "https://mcp.cloudflare.com/mcp",
            "authorize_url": "https://mcp.cloudflare.com/authorize",
            "token_url": "https://mcp.cloudflare.com/token",
            "registration_endpoint": "https://mcp.cloudflare.com/register",
            "token_endpoint_auth_method": "none",
            "resource": "https://mcp.cloudflare.com/mcp",
            "issuer": "https://mcp.cloudflare.com",
            "scopes": ["user:read", "account:read", "offline_access"],
            "setup_hints": [
                (
                    "Authorize opens Cloudflare OAuth. Agent skills from "
                    "[cloudflare/skills](https://github.com/cloudflare/skills) stay in the IDE; "
                    "this app is remote MCP at mcp.cloudflare.com. Setup: "
                    "[agent-setup/prompt.md](https://developers.cloudflare.com/agent-setup/prompt.md)."
                ),
            ],
        },
    },
    {
        "slug": "aliyun",
        "label": "阿里云",
        "name": "阿里云",
        "description": (
            "Alibaba Cloud OpenAPI MCP via local mcp-proxy — paste AccessKey ID and Secret "
            "at Connect (no OAuth)."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Developer Tools"],
        "tags": ["aliyun", "alibaba", "stdio", "mcp", "openapi"],
        "sort_order": 17,
        "spec": {
            "command": "uvx",
            "args": ["alibabacloud.mcp-proxy==0.2.17"],
            "env": [
                {
                    "name": "ALIBABA_CLOUD_ACCESS_KEY_ID",
                    "label": "AccessKey ID",
                    "secret": True,
                },
                {
                    "name": "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                    "label": "AccessKey Secret",
                    "secret": True,
                },
            ],
            "setup_hints": [
                (
                    "Create an AccessKey for a RAM user with "
                    "AliyunOpenAPIMCPServerStaticCredentialAccess "
                    "(or ram:GenerateAccessToken + openapiexplorer:*). Docs: "
                    "[alibabacloud-api-mcp-server]"
                    "(https://github.com/aliyun/alibabacloud-api-mcp-server)."
                ),
            ],
        },
    },
    {
        "slug": "vultr",
        "label": "Vultr",
        "name": "Vultr",
        "description": (
            "Vultr hosted MCP (read-only infrastructure, billing, and account tools) via OAuth."
        ),
        "connection": "oauth",
        "popular": True,
        "categories": ["Developer Tools"],
        "tags": ["vultr", "oauth", "mcp", "cloud"],
        "sort_order": 18,
        "spec": {
            "url": "https://mcp.vultr.com/mcp",
            "authorize_url": "https://mcp.vultr.com/authorize",
            "token_url": "https://mcp.vultr.com/token",
            "registration_endpoint": "https://mcp.vultr.com/register",
            "token_endpoint_auth_method": "none",
            "resource": "https://mcp.vultr.com/mcp",
            "issuer": "https://mcp.vultr.com",
            "scopes": [],
            "setup_hints": [
                (
                    "Org admin must enable MCP and attach the Use MCP policy. Docs: "
                    "[Vultr MCP overview]"
                    "(https://docs.vultr.com/reference/mcp-server/overview)."
                ),
            ],
        },
    },
    {
        "slug": "vercel",
        "label": "Vercel",
        "name": "Vercel",
        "description": (
            "Vercel official remote MCP (projects, deployments, docs, analytics) via OAuth."
        ),
        "connection": "oauth",
        "popular": True,
        "categories": ["Developer Tools"],
        "tags": ["vercel", "oauth", "mcp", "deploy"],
        "sort_order": 19,
        "spec": {
            "url": "https://mcp.vercel.com",
            "authorize_url": "https://vercel.com/oauth/authorize",
            "token_url": "https://vercel.com/api/login/oauth/token",
            "registration_endpoint": "https://vercel.com/api/login/oauth/register",
            "token_endpoint_auth_method": "none",
            "resource": "https://mcp.vercel.com",
            "issuer": "https://vercel.com",
            "scopes": ["openid"],
            "setup_hints": [
                (
                    "Vercel may require an approved MCP client or CIMD client_id. Docs: "
                    "[vercel.com/docs/agent-resources/vercel-mcp]"
                    "(https://vercel.com/docs/agent-resources/vercel-mcp)."
                ),
            ],
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
            # Dropbox only returns refresh_token when token_access_type=offline.
            "authorize_url": "https://www.dropbox.com/oauth2/authorize?token_access_type=offline",
            "token_url": "https://api.dropboxapi.com/oauth2/token",
            "token_endpoint_auth_method": "client_secret_basic",
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
            "setup_hints": [
                "Get a token at [weiyun.com/act/openclaw](https://www.weiyun.com/act/openclaw).",
            ],
        },
    },
    {
        "slug": "raindrop",
        "label": "Raindrop.io",
        "name": "Raindrop.io",
        "description": "Raindrop bookmarks via remote MCP (Bearer API token; Pro beta).",
        "connection": "http",
        "popular": True,
        "categories": ["Productivity"],
        "tags": ["raindrop", "bookmarks", "http"],
        "sort_order": 52,
        "spec": {
            "url": "https://api.raindrop.io/rest/v2/ai/mcp",
            "setup_hints": [
                (
                    "Paste a Raindrop API access token (Bearer). Docs: "
                    "[developer.raindrop.io/mcp](https://developer.raindrop.io/mcp). "
                    "MCP is Pro-beta."
                ),
            ],
        },
    },
    {
        "slug": "youdaonote",
        "label": "有道云笔记",
        "name": "有道云笔记",
        "description": (
            "Youdao Note via remote MCP (x-api-key). Same API Key as youdaonote CLI / Skills."
        ),
        "connection": "http",
        "popular": True,
        "categories": ["Productivity"],
        "tags": ["youdaonote", "notes", "http", "netease"],
        "sort_order": 53,
        "spec": {
            "url": "https://open.mail.163.com/api/ynote/mcp/sse",
            "auth_header": "x-api-key",
            "token_prefix": "",
            "setup_hints": [
                (
                    "Paste the API Key from the "
                    "[NetEase MCP console](https://mopen.163.com) "
                    "(phone-bound Youdao Note account). Guides: "
                    "[CLI](https://note.youdao.com/help-center/cli-guide.html), "
                    "[Skills](https://note.youdao.com/help-center/skill-guide.html)."
                ),
            ],
        },
    },
    {
        "slug": "weknora",
        "label": "WeKnora",
        "name": "WeKnora",
        "description": (
            "Tencent WeKnora self-hosted knowledge MCP. Paste your /mcp/<endpoint_id> URL "
            "and Bearer endpoint token (settings → 发布集成 → MCP Server)."
        ),
        "connection": "http",
        "popular": True,
        "categories": ["Productivity"],
        "tags": ["weknora", "tencent", "knowledge", "rag", "http"],
        "sort_order": 54,
        "spec": {
            "url_from_operator": True,
            "setup_hints": [
                (
                    "In WeKnora: 设置 → 发布集成 → MCP Server. Copy the endpoint URL and "
                    "Bearer token (mcp_…). Docs: "
                    "[github.com/Tencent/WeKnora](https://github.com/Tencent/WeKnora)."
                ),
            ],
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
        "slug": "cubox",
        "label": "Cubox",
        "name": "Cubox",
        "description": "Cubox library via skill-bridge (official cubox-cli; paste API Extension token).",
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Reading"],
        "tags": ["cubox", "bookmarks", "json_cli"],
        "sort_order": 72,
        "spec": {
            "manifest": CUBOX_MANIFEST,
            "secrets": [
                {
                    "name": "CUBOX_TOKEN",
                    "label": "Cubox API token (or full API Extension URL)",
                    "secret": True,
                },
            ],
            "env_defaults": {
                "CUBOX_SERVER": "cubox.pro",
            },
            "setup_hints": [
                (
                    "Enable API Extension, then paste the full save URL from "
                    "[cubox.pro](https://cubox.pro/web/settings/extensions) or "
                    "[cubox.cc](https://cubox.cc/web/settings/extensions) "
                    "(a full URL also sets CUBOX_SERVER; a bare token keeps the cubox.pro default). "
                    "Docs: [help.cubox.pro/ai/agents](https://help.cubox.pro/ai/agents/)."
                ),
            ],
        },
    },
    {
        "slug": "ima",
        "label": "IMA",
        "name": "IMA",
        "description": (
            "Tencent IMA notes and knowledge base via OpenAPI "
            "(Client ID + API Key from ima.qq.com/agent-interface)."
        ),
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Productivity"],
        "tags": ["ima", "notes", "knowledge", "http_rpc"],
        "sort_order": 74,
        "spec": {
            "manifest": IMA_MANIFEST,
            "secrets": [
                {
                    "name": "IMA_OPENAPI_CLIENTID",
                    "label": "IMA Client ID",
                    "secret": True,
                },
                {
                    "name": "IMA_OPENAPI_APIKEY",
                    "label": "IMA API Key",
                    "secret": True,
                },
            ],
            "setup_hints": [
                (
                    "Create Client ID + API Key at "
                    "[ima.qq.com/agent-interface](https://ima.qq.com/agent-interface)."
                ),
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
        "slug": "qqmusic",
        "label": "QQ 音乐",
        "name": "QQ 音乐",
        "description": (
            "QQ Music search, charts, playlists, and listening reports via "
            "skill-bridge (API key from y.qq.com)."
        ),
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Music"],
        "tags": ["qqmusic", "music", "http_rpc"],
        "sort_order": 76,
        "spec": {
            "manifest": QQMUSIC_MANIFEST,
            "secrets": [
                {
                    "name": "QQMUSIC_API_KEY",
                    "label": "QQ Music API key (qmk-…)",
                    "secret": True,
                },
            ],
            "setup_hints": [
                (
                    "Create an API key at "
                    "[y.qq.com/n/ryqq_v2/qqmusic_skills]"
                    "(https://y.qq.com/n/ryqq_v2/qqmusic_skills). "
                    "Docs: "
                    "[github.com/tencentmusic/qqmusic-skills]"
                    "(https://github.com/tencentmusic/qqmusic-skills)."
                ),
            ],
        },
    },
    {
        "slug": "flyai",
        "label": "飞猪 FlyAI",
        "name": "飞猪 FlyAI",
        "description": (
            "Fliggy travel search (flights, hotels, trains, POIs, Marriott) via skill-bridge "
            "wrapping @fly-ai/flyai-cli."
        ),
        "connection": "skill_bridge",
        "popular": True,
        "categories": ["Travel"],
        "tags": ["flyai", "fliggy", "travel", "json_cli"],
        "sort_order": 77,
        "spec": {
            "manifest": FLYAI_MANIFEST,
            "secrets": [
                {
                    "name": "FLYAI_API_KEY",
                    "label": "FlyAI API token",
                    "secret": True,
                },
            ],
            "setup_hints": [
                (
                    "Paste your FlyAI API token at Connect. Skill: "
                    "[alibaba-flyai/flyai-skill]"
                    "(https://github.com/alibaba-flyai/flyai-skill)."
                ),
            ],
        },
    },
    {
        "slug": "box",
        "label": "Box",
        "name": "Box",
        "description": (
            "Box files via rclone (paste your `rclone config show box` output at "
            "Connect; authorize first with `rclone authorize \"box\"`)."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["box", "files", "storage", "rclone"],
        "sort_order": 26,
        "spec": {
            "command": RCLONE_SHIM,
            "args": [],
            "env": [
                {
                    "name": "RCLONE_REMOTE_CONFIG",
                    "label": (
                        "rclone remote config — run `rclone config show box` "
                        "(authorize first with `rclone authorize \"box\"`)"
                    ),
                    "secret": True,
                    "multiline": True,
                },
            ],
        },
    },
    {
        "slug": "gdrive",
        "label": "Google Drive",
        "name": "Google Drive",
        "description": (
            "Google Drive via the official remote MCP (drivemcp.googleapis.com). "
            "Paste OAuth Client ID/secret at Connect (no DCR)."
        ),
        "connection": "oauth",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["gdrive", "google", "drive", "mcp", "oauth"],
        "sort_order": 34,
        "spec": {
            "url": "https://drivemcp.googleapis.com/mcp/v1",
            # access_type=offline + prompt=consent so Google returns a refresh token.
            "authorize_url": (
                "https://accounts.google.com/o/oauth2/v2/auth"
                "?access_type=offline&prompt=consent"
            ),
            "token_url": "https://oauth2.googleapis.com/token",
            "token_endpoint_auth_method": "client_secret_post",
            "resource": "https://drivemcp.googleapis.com/mcp/v1",
            "scopes": [
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/drive.file",
            ],
            "setup_hints": [
                (
                    "Enable Drive API + Drive MCP API, create a Web OAuth client, "
                    "check **Use this client for an AI-powered agent**, and set the "
                    "redirect URI shown at Connect. Docs: "
                    "[Drive MCP setup]"
                    "(https://developers.google.com/workspace/drive/api/guides/"
                    "configure-mcp-server)."
                ),
            ],
        },
    },
    {
        "slug": "onedrive",
        "label": "OneDrive",
        "name": "OneDrive",
        "description": (
            "OneDrive files via rclone (paste your `rclone config show onedrive` "
            "output at Connect; tokens stay encrypted in the gateway vault)."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["onedrive", "microsoft", "files", "rclone"],
        "sort_order": 36,
        "spec": {
            "command": RCLONE_SHIM,
            "args": [],
            "env": [
                {
                    "name": "RCLONE_REMOTE_CONFIG",
                    "label": (
                        "rclone remote config — run `rclone config show onedrive` "
                        "(authorize first with `rclone authorize \"onedrive\"`)"
                    ),
                    "secret": True,
                    "multiline": True,
                },
            ],
        },
    },
    {
        "slug": "yandex",
        "label": "Yandex Disk",
        "name": "Yandex Disk",
        "description": (
            "Yandex Disk files via rclone (paste your `rclone config show yandex` "
            "output at Connect; authorize first with `rclone authorize \"yandex\"`)."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["yandex", "disk", "files", "rclone"],
        "sort_order": 37,
        "spec": {
            "command": RCLONE_SHIM,
            "args": [],
            "env": [
                {
                    "name": "RCLONE_REMOTE_CONFIG",
                    "label": (
                        "rclone remote config — run `rclone config show yandex` "
                        "(authorize first with `rclone authorize \"yandex\"`)"
                    ),
                    "secret": True,
                    "multiline": True,
                },
            ],
        },
    },
    {
        "slug": "drime",
        "label": "Drime",
        "name": "Drime",
        "description": (
            "Drime Cloud files via rclone (create an API token in Drime Settings → "
            "Developers, configure a `drime` remote, then paste `rclone config show drime` "
            "at Connect). Requires rclone ≥ 1.73."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["drime", "files", "storage", "rclone"],
        "sort_order": 38,
        "spec": {
            "command": RCLONE_SHIM,
            "args": [],
            "env": [
                {
                    "name": "RCLONE_REMOTE_CONFIG",
                    "label": (
                        "rclone remote config — run `rclone config show drime` "
                        "(type=drime + access_token from Drime Settings → Developers)"
                    ),
                    "secret": True,
                    "multiline": True,
                },
            ],
        },
    },
    {
        "slug": "mega",
        "label": "MEGA",
        "name": "MEGA",
        "description": (
            "MEGA cloud storage via rclone (paste `rclone config show mega` — prefer "
            "session_id+master_key; user/pass also works)."
        ),
        "connection": "stdio",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["mega", "files", "storage", "rclone"],
        "sort_order": 39,
        "spec": {
            "command": RCLONE_SHIM,
            "args": [],
            "env": [
                {
                    "name": "RCLONE_REMOTE_CONFIG",
                    "label": (
                        "rclone remote config — `rclone config show mega` "
                        "(prefer session_id+master_key; user/pass ok)"
                    ),
                    "secret": True,
                    "multiline": True,
                },
            ],
            "setup_hints": [
                (
                    "Paste the full output of rclone config show mega (obscured pass is fine). "
                    "Docs: [rclone.org/mega](https://rclone.org/mega/)."
                ),
            ],
        },
    },
    {
        "slug": "pikpak",
        "label": "PikPak",
        "name": "PikPak",
        "description": "PikPak cloud drive via the official remote MCP (OAuth 2.1 + PKCE).",
        "connection": "oauth",
        "popular": True,
        "categories": ["Storage"],
        "tags": ["pikpak", "drive", "mcp", "oauth"],
        "sort_order": 78,
        "spec": {
            "url": "https://pikpak.ai/mcp",
            "authorize_url": "https://mypikpak.com/drive/auth-center",
            "token_url": "https://user.mypikpak.com/v1/auth/token",
            # Exact RFC 9207 iss validation (the AS advertises its issuer identifier).
            "issuer": "https://user.mypikpak.com",
            "resource": "https://pikpak.ai/mcp",
            "scopes": [
                "files:read", "files:write", "files:manage", "files:purge",
                "cloud_download", "share", "account",
            ],
            # Public client; DCR is available when no client_id is configured.
            "registration_endpoint": "https://user.mypikpak.com/v1/clients-registrations/openid-connect",
            "token_endpoint_auth_method": "none",
        },
    },
]

for _app in DEFAULT_APPS:
    _app.setdefault("egress", _default_egress_for_slug(str(_app.get("slug", ""))))


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


def _require_https(spec: dict[str, Any], key: str) -> str:
    val = _require_str(spec, key)
    if not val.startswith("https://"):
        raise RegistryError(422, "invalid_request", f"spec.{key} must be an https URL")
    return val


# Connect drawer presentation: optional per-app prose in [spec]. Caps mirror
# _normalize_labels style. Link targets inside [label](url) must be https://.
SETUP_HINTS_MAX = 8
SETUP_HINTS_MAX_CHARS = 400
SETUP_HINTS_MAX_TOTAL = 2048
_SETUP_HINT_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _normalize_setup_hints(spec: dict[str, Any]) -> list[str] | None:
    """Return normalized hints, or None when the key is absent (omit from out)."""
    if "setup_hints" not in spec:
        return None
    raw = spec.get("setup_hints")
    if not isinstance(raw, list):
        raise RegistryError(422, "invalid_request", "spec.setup_hints must be a list of strings")
    if len(raw) > SETUP_HINTS_MAX:
        raise RegistryError(
            422, "invalid_request", f"spec.setup_hints supports at most {SETUP_HINTS_MAX} entries"
        )
    out: list[str] = []
    total = 0
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RegistryError(
                422, "invalid_request", "spec.setup_hints must be a list of non-empty strings"
            )
        if any(ord(ch) < 32 and ch not in "\t\n" for ch in item):
            raise RegistryError(
                422, "invalid_request", "spec.setup_hints entries must not contain control characters"
            )
        text = item.strip()
        if len(text) > SETUP_HINTS_MAX_CHARS:
            raise RegistryError(
                422,
                "invalid_request",
                f"spec.setup_hints entries must be ≤{SETUP_HINTS_MAX_CHARS} chars",
            )
        for match in _SETUP_HINT_LINK_RE.finditer(text):
            href = match.group(2).strip()
            if not href.startswith("https://"):
                raise RegistryError(
                    422,
                    "invalid_request",
                    "spec.setup_hints link targets must be https:// URLs",
                )
        total += len(text)
        if total > SETUP_HINTS_MAX_TOTAL:
            raise RegistryError(
                422,
                "invalid_request",
                f"spec.setup_hints total size must be ≤{SETUP_HINTS_MAX_TOTAL} bytes",
            )
        out.append(text)
    return out


def _with_setup_hints(out: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    hints = _normalize_setup_hints(spec)
    if hints is not None:
        out["setup_hints"] = hints
    return out


def validate_spec(connection: str, spec: Any) -> dict[str, Any]:
    if connection not in CATALOG_CONNECTIONS:
        raise RegistryError(400, "unsupported", f"connection {connection!r} is not supported")
    if not isinstance(spec, dict):
        raise RegistryError(422, "invalid_request", "spec must be an object")
    if connection == "oauth":
        out: dict[str, Any] = {
            "url": _require_https(spec, "url"),
            "authorize_url": _require_https(spec, "authorize_url"),
            "token_url": _require_https(spec, "token_url"),
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
        for opt in ("resource", "registration_endpoint", "issuer"):
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
        return _with_setup_hints(out, spec)
    if connection == "http":
        # Self-hosted MCP (e.g. WeKnora): operator pastes endpoint URL at Connect.
        url_from_operator = bool(spec.get("url_from_operator"))
        out: dict[str, Any] = {}
        if url_from_operator:
            out["url_from_operator"] = True
            url_hint = spec.get("url")
            if url_hint is not None and url_hint != "":
                if not isinstance(url_hint, str) or not url_hint.strip():
                    raise RegistryError(
                        422, "invalid_request", "spec.url must be a non-empty string when set"
                    )
                out["url"] = url_hint.strip()
        else:
            out["url"] = _require_str(spec, "url")
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
        return _with_setup_hints(out, spec)
    if connection == "webdav":
        raw_args = spec.get("args") or WEBDAV_MCP_ARGS
        if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
            raise RegistryError(422, "invalid_request", "spec.args must be a string list")
        out = {
            "root_url": _require_str(spec, "root_url"),
            "root_path": str(spec.get("root_path") or "/").strip() or "/",
            "package": str(spec.get("package") or WEBDAV_MCP_PACKAGE),
            "args": list(raw_args),
        }
        return _with_setup_hints(out, spec)
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
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise RegistryError(422, "invalid_request", "spec.env entries need a name")
        name = item["name"].strip()
        if not name:
            raise RegistryError(422, "invalid_request", "spec.env entries need a name")
        env_out.append(
            {
                "name": name,
                "label": str(item.get("label") or name),
                "secret": bool(item.get("secret", True)),
                # Multi-line values (rclone config paste) render as a textarea.
                "multiline": item.get("multiline") is True,
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
    return _with_setup_hints(out, spec)


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
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise RegistryError(422, "invalid_request", "spec.secrets entries need a name")
        name = name.strip()
        secrets_out.append(
            {
                "name": name,
                "label": str(item.get("label") or name),
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
        for env_name in (manifest.auth.extra_headers_env or {}).values():
            if env_name not in names:
                raise RegistryError(
                    422,
                    "invalid_request",
                    f"spec.secrets must include auth extra_headers_env {env_name!r}",
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
    args = spec.get("args") or SKILL_BRIDGE_ARGS
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise RegistryError(422, "invalid_request", "spec.args must be a string list")
    return _with_setup_hints(
        {
            "manifest": manifest.model_dump(mode="json"),
            "secrets": secrets_out,
            "env_defaults": env_out,
            "command": str(spec.get("command") or SKILL_BRIDGE_COMMAND),
            "args": list(args),
        },
        spec,
    )


def _merge_skill_bridge_seed(live: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
    """Refresh the packaged manifest without discarding operator-owned spec fields.

    Managed from the seed: the manifest (tools, auth, backend). Preserved from the
    live row: extra secrets, command/args overrides, and env_defaults the operator
    already set. Seed secrets and env_defaults fill keys the live row does not have.
    """
    merged: dict[str, Any] = dict(live or {})
    seed_manifest = seed.get("manifest")
    if isinstance(seed_manifest, dict):
        merged["manifest"] = seed_manifest
    # Presentation-only: fill from seed when the live row never had hints.
    if "setup_hints" not in merged and "setup_hints" in seed:
        merged["setup_hints"] = seed["setup_hints"]

    secrets: list[dict[str, Any]] = []
    seen: set[Any] = set()
    live_secrets = merged.get("secrets") if isinstance(merged.get("secrets"), list) else []
    seed_secrets = seed.get("secrets") if isinstance(seed.get("secrets"), list) else []
    for item in [*live_secrets, *seed_secrets]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name in seen:
            continue
        seen.add(name)
        secrets.append(item)
    merged["secrets"] = secrets

    live_defaults = merged.get("env_defaults")
    defaults = dict(live_defaults) if isinstance(live_defaults, dict) else {}
    seed_defaults = seed.get("env_defaults") if isinstance(seed.get("env_defaults"), dict) else {}
    for key, value in seed_defaults.items():
        defaults.setdefault(key, value)
    merged["env_defaults"] = defaults

    command = merged.get("command")
    if not isinstance(command, str) or not command.strip():
        merged["command"] = seed.get("command")
    args = merged.get("args")
    if not isinstance(args, list) or not args:
        merged["args"] = list(seed.get("args") or [])
    return merged


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

        # Idempotent taxonomy + setup_hints sync for deploys that already have seed rows.
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
                seed_spec = seed.get("spec") if isinstance(seed.get("spec"), dict) else {}
                seed_hints = seed_spec.get("setup_hints") if isinstance(seed_spec, dict) else None
                live_spec = dict(row.spec or {})
                if (
                    isinstance(seed_hints, list)
                    and seed_hints
                    and "setup_hints" not in live_spec
                ):
                    try:
                        row.spec = validate_spec(
                            row.connection, {**live_spec, "setup_hints": seed_hints}
                        )
                        dirty = True
                    except RegistryError:
                        log.warning(
                            "setup_hints backfill skipped for %s (invalid seed)",
                            row.slug,
                            exc_info=True,
                        )
            if dirty:
                await s.commit()

        # Idempotent connector migration: catalog rows still on a retired shape
        # (oauth / ms-365) while the seed is stdio. CatalogStore.update cannot
        # change connection, so rewrite here and bump linked servers to respawn.
        async with self._sf() as s:
            seed_by_slug = {
                a["slug"]: a
                for a in DEFAULT_APPS
                if a.get("connection") == "stdio"
                and a["slug"] in ("box", "onedrive", "aliyun", "tencentcloud")
            }
            rows = (
                await s.execute(select(CatalogApp).where(CatalogApp.slug.in_(list(seed_by_slug))))
            ).scalars().all()
            migrated = 0
            for row in rows:
                seed = seed_by_slug[row.slug]
                # Narrow: only migrate rows still on a retired connector shape.
                # The ms-365 marker lives in spec.args (command was bare `npx`);
                # oauth seeds (box / aliyun / tencentcloud) are caught by connection.
                stale = row.connection == "oauth" or "ms-365-mcp-server" in str(
                    row.spec.get("args") or ""
                )
                if not stale:
                    continue
                seed_spec = seed["spec"]
                seed_command = str(seed_spec.get("command") or "")
                seed_args = list(seed_spec.get("args") or [])
                row.connection = "stdio"
                row.label = seed["label"]
                row.name = seed["name"]
                row.description = seed["description"]
                row.tags = _normalize_labels(seed.get("tags"), field="tags")
                row.spec = validate_spec("stdio", seed_spec)
                linked = (
                    await s.execute(select(Server).where(Server.slug == row.slug))
                ).scalars().all()
                for srv in linked:
                    # Targeted cleanup: drop only entries belonging to the retired
                    # connectors (OneDrive MS365_* env; remote Bearer / OAuth client
                    # secret). A blanket NOT IN (seed env) delete would silently
                    # discard user env entries.
                    retired = {"Authorization", "portunus.oauth_client_secret"}
                    retired |= {f"MS365_MCP_{k}" for k in (
                        "TENANT_ID", "USE_KEYTAR", "TOKEN_CACHE_PATH",
                        "SELECTED_ACCOUNT_PATH", "CLIENT_ID", "CLIENT_SECRET",
                    )}
                    stale_env = (
                        await s.execute(
                            select(VaultEntry).where(
                                VaultEntry.server_id == srv.id,
                                VaultEntry.name.in_(sorted(retired)),
                            )
                        )
                    ).scalars().all()
                    for entry in stale_env:
                        await s.delete(entry)
                    if srv.kind == "stdio":
                        srv.command = seed_command
                        srv.args = list(seed_args)
                    elif srv.kind == "remote":
                        srv.kind = "stdio"
                        srv.command = seed_command
                        srv.args = list(seed_args)
                        srv.url = None
                        srv.oauth = None
                        srv.has_oauth_refresh = False
                        srv.token_expires_at = None
                        srv.enabled = False
                    srv.vault_version = int(srv.vault_version or 0) + 1
                migrated += 1
            if migrated:
                await s.commit()
                log.info("stdio seed migration: refreshed %d catalog row(s)", migrated)

        # gdrive: rclone stdio → official Drive MCP oauth (drivemcp.googleapis.com).
        async with self._sf() as s:
            seed = next((a for a in DEFAULT_APPS if a["slug"] == "gdrive"), None)
            row = (
                await s.execute(select(CatalogApp).where(CatalogApp.slug == "gdrive"))
            ).scalar_one_or_none()
            if (
                seed is not None
                and row is not None
                and seed.get("connection") == "oauth"
                and isinstance(row.spec, dict)
                and row.spec.get("command") == RCLONE_SHIM
            ):
                row.connection = "oauth"
                row.description = seed["description"]
                row.tags = _normalize_labels(seed.get("tags"), field="tags")
                row.spec = validate_spec("oauth", seed["spec"])
                linked = (
                    await s.execute(select(Server).where(Server.slug == "gdrive"))
                ).scalars().all()
                for srv in linked:
                    # Only rewrite the rclone stdio shape; leave custom / already-remote
                    # gdrive servers alone.
                    if srv.kind != "stdio" or srv.command != RCLONE_SHIM:
                        continue
                    # Drop rclone paste + any prior auth; operator re-Connects with
                    # Google OAuth Client ID/secret.
                    stale_env = (
                        await s.execute(
                            select(VaultEntry).where(
                                VaultEntry.server_id == srv.id,
                                VaultEntry.name.in_(
                                    sorted(
                                        {
                                            "RCLONE_REMOTE_CONFIG",
                                            "Authorization",
                                            "portunus.oauth_client_secret",
                                            "portunus.oauth_refresh_token",
                                        }
                                    )
                                ),
                            )
                        )
                    ).scalars().all()
                    for entry in stale_env:
                        await s.delete(entry)
                    await s.execute(delete(Tool).where(Tool.server_id == srv.id))
                    srv.kind = "remote"
                    srv.command = None
                    srv.args = []
                    srv.url = seed["spec"]["url"]
                    srv.oauth = {
                        k: v for k, v in row.spec.items() if k not in {"url", "setup_hints"}
                    }
                    srv.has_oauth_refresh = False
                    srv.token_expires_at = None
                    srv.enabled = False
                    srv.vault_version = int(srv.vault_version or 0) + 1
                await s.commit()
                log.info("gdrive migration: switched catalog row to Drive MCP oauth")

        # Idempotent: add new skill_bridge seeds (e.g. weread) on existing deploys,
        # and refresh skill_bridge manifests when the packaged seed grows (tool list).
        # Also insert missing rclone-shim stdio seeds (yandex/drime/…) and http
        # seeds (raindrop/…) — same gap as skill_bridge: empty-DB seed only runs once.
        added = 0
        refreshed = 0
        async with self._sf() as s:
            for raw in DEFAULT_APPS:
                connection = raw.get("connection")
                # Insert missing packaged stdio seeds (rclone shim + pinned uvx/npx).
                is_packaged_stdio = (
                    connection == "stdio"
                    and isinstance(raw.get("spec"), dict)
                    and raw["spec"].get("command") in (RCLONE_SHIM, "uvx", "npx")
                )
                if (
                    connection not in ("skill_bridge", "http", "oauth")
                    and not is_packaged_stdio
                ):
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
                if connection != "skill_bridge":
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
                seed_secret_names = {
                    s.get("name")
                    for s in (spec.get("secrets") or [])
                    if isinstance(s, dict) and s.get("name")
                }
                live_secret_names = {
                    s.get("name")
                    for s in ((row.spec or {}).get("secrets") or [])
                    if isinstance(s, dict) and s.get("name")
                }
                secrets_changed = bool(seed_secret_names - live_secret_names)
                spec_changed = (
                    (seed_names and seed_names != live_names)
                    or auth_changed
                    or defaults_changed
                    or secrets_changed
                )
                if spec_changed:
                    live = dict(row.spec or {})
                    merged = validate_spec("skill_bridge", _merge_skill_bridge_seed(live, spec))
                    if merged == live:
                        continue
                    row.spec = merged
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

        # Normalize legacy host-specific overseas-* tags to portable egress tags.
        # Do NOT refill empty preferred_regions: empty means operator chose "Any"
        # (create_server already applies catalog defaults when regions are omitted).
        # recipe_slug is set only by explicit catalog Connect — never inferred by slug.
        regions_fixed = 0
        affinity_slugs = sorted(OVERSEAS_EGRESS_SLUGS | DOMESTIC_EGRESS_SLUGS)
        async with self._sf() as s:
            linked = (
                await s.execute(select(Server).where(Server.slug.in_(affinity_slugs)))
            ).scalars().all()
            for srv in linked:
                hint = preferred_regions_for_slug(srv.slug)
                if not hint:
                    continue
                cur = list(srv.preferred_regions or [])
                if not any(r.startswith("overseas-") for r in cur):
                    continue
                if cur == hint:
                    continue
                srv.preferred_regions = hint
                regions_fixed += 1
            if regions_fixed:
                await s.commit()
                log.info(
                    "egress preferred_regions backfill: %d server(s)", regions_fixed
                )

        return inserted + added + refreshed

    async def list_apps(self) -> list[dict[str, Any]]:
        from .toml_catalog import catalog_mode, get_loaded, recipe_public_view

        loaded = get_loaded()
        mode = catalog_mode()
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(CatalogApp).order_by(CatalogApp.sort_order, CatalogApp.slug)
                )
            ).scalars().all()
            by_slug = {r.slug: r for r in rows}
            if loaded is None:
                return [{**catalog_view(r), "catalog_mode": mode} for r in rows]
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            for recipe in sorted(
                (r for r in loaded.recipes if r.enabled),
                key=lambda r: (r.sort_order, r.slug),
            ):
                db = by_slug.get(recipe.slug)
                app_id: str | None
                if mode == "B":
                    app_id = recipe.slug
                else:
                    app_id = str(db.id) if db is not None else None
                view = recipe_public_view(recipe, catalog_mode=mode, id=app_id)
                # SPA Connect still needs skill_bridge manifest body when present in DB seed shape
                if db is not None and recipe.connection == "skill_bridge":
                    merged_spec = dict(view["spec"])
                    if "manifest" in (db.spec or {}) and "manifest" not in merged_spec:
                        merged_spec["manifest"] = db.spec["manifest"]
                    view["spec"] = merged_spec
                out.append(view)
                seen.add(recipe.slug)
            # DB-only apps not yet migrated to TOML remain visible in mode A
            if mode == "A":
                for r in rows:
                    if r.slug not in seen:
                        out.append({**catalog_view(r), "catalog_mode": mode})
                out.sort(key=lambda v: (v.get("sort_order", 0), v["slug"]))
            return out

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
