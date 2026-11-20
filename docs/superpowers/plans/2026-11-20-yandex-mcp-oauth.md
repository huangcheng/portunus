# Yandex Disk + Mail MCP OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace rclone Yandex Disk with official Yandex 360 MCP + browser OAuth, and add Yandex Mail the same way.

**Architecture:** Remote Streamable HTTP MCP (`mcp.360.yandex.net`) with operator-registered Yandex OAuth apps. Token endpoints return `token_type: bearer`; Portunus rewrites vaulted `Authorization` to `OAuth <token>` via `authorization_scheme`. Authorize `scope` uses comma delimiter per Yandex ID docs.

**Tech Stack:** Portunus gateway OAuth (`oauth.py` / `registry.py` / `catalog.py`), catalog TOML, dashboard icons.

**Spec:** `docs/superpowers/specs/2026-11-20-yandex-mcp-oauth-design.md`

## Global Constraints

- No baked `client_id` (paste at Connect).
- Drop rclone for `yandex`; keep rclone for box/drime/mega.
- MCP auth header scheme is literally `OAuth` (capital O).
- Yandex authorize `scope` values are comma-separated.
- Separate grants: Disk scopes vs Mail scopes; separate catalog cards.

## Review Focus

- Token exchange stores `OAuth …` not `Bearer …` when scheme is set.
- Refresh path re-applies the same scheme.
- Authorize URL joins scopes with `,` not spaces.
- Existing rclone `yandex` servers migrate to remote oauth and require re-Connect.
- `canonicalize_authorization` must not rewrite `OAuth` to Bearer.

---

### Task 1: OAuth authorization_scheme + scope_delimiter

**Files:**
- Modify: `gateway/portunus_gateway/oauth.py`
- Modify: `gateway/portunus_gateway/catalog.py` (`validate_spec` oauth branch)
- Modify: `gateway/portunus_gateway/registry.py` (`store_oauth_tokens`)
- Modify: `gateway/portunus_gateway/operator_api.py` (`store.start` call)
- Test: `gateway/tests/test_oauth.py`

**Interfaces:**
- Produces: `apply_authorization_scheme(authorization: str, scheme: str | None) -> str`
- Produces: `validate_oauth_config` / `validate_spec("oauth")` accept `authorization_scheme` (token: printable ASCII, no SP/CTL) and `scope_delimiter` (`" "` or `","`)
- Produces: `OAuthStore.start(..., scope_delimiter: str = " ")`
- Consumes (Task 2): vault writes go through `store_oauth_tokens`, which applies `server.oauth["authorization_scheme"]`

- [ ] **Step 1: Failing tests** — `test_apply_authorization_scheme_rewrites_bearer_to_oauth`; `test_validate_oauth_config_accepts_scheme_and_comma_delimiter`; `test_oauth_store_start_joins_scopes_with_comma`

- [ ] **Step 2: Implement helpers + validate + start + store_oauth_tokens apply**

- [ ] **Step 3: Run** `cd gateway && uv run pytest tests/test_oauth.py -k "authorization_scheme or scope_delimiter or canonicalize or validate_oauth" -v`

---

### Task 2: Catalog seeds, TOML, migration, icons, tests

**Files:**
- Modify: `gateway/portunus_gateway/catalog.py` (DEFAULT_APPS `yandex`; insert `yandex-mail`; `ensure_defaults` rclone→oauth like onedrive; add `yandex-mail` to `OVERSEAS_EGRESS_SLUGS`)
- Modify: `catalog/apps/yandex.toml`
- Create: `catalog/apps/yandex-mail.toml`
- Modify: `dashboard/src/icons/apps.tsx` (map `yandex-mail` → `yandex` mark)
- Modify: `dashboard/src/style.css` if needed (reuse yandex 48×48)
- Modify: `gateway/tests/test_skill_bridge_catalog.py`

**Seed values:**

Disk (`yandex`):
- url `https://mcp.360.yandex.net/v1/disk/mcp`
- authorize `https://oauth.yandex.ru/authorize`
- token `https://oauth.yandex.ru/token`
- `token_endpoint_auth_method: client_secret_post`
- `authorization_scheme: OAuth`
- `scope_delimiter: ,`
- scopes: `cloud_api:disk.read`, `cloud_api:disk.write`, `cloud_api:disk.info`
- no client_id; egress overseas; sort_order 37

Mail (`yandex-mail`):
- url `https://mcp.360.yandex.net/v1/mail/mcp`
- same OAuth AS / scheme / delimiter
- scopes: `mail:messages.read`, `mail:messages.write`, `mail:folders.read`, `mail:folders.write`, `mail:labels.read`, `mail:labels.write`
- sort_order ~37.5 → use 35 or 39; pick **39** (after mega 38) or insert as 37.5 integer — use **35** between outlook and onedrive? Prefer **39** after mega to avoid reshuffling.
- setup_hints: Yandex 360 subscription; no send via MCP; register at oauth.yandex.ru; paste Client ID/secret; set redirect URI from Connect

Migration: copy onedrive block for slug `yandex` (stdio/RCLONE_SHIM → oauth).

- [ ] **Step 1: Failing tests** — replace rclone assertion for yandex; add `test_yandex_oauth_mcp_seeds_shape`; keep box/drime/mega on rclone

- [ ] **Step 2: Implement seeds + TOML + migration + icon map**

- [ ] **Step 3: Run** `cd gateway && uv run pytest tests/test_skill_bridge_catalog.py tests/test_toml_catalog.py tests/test_oauth.py -v`
