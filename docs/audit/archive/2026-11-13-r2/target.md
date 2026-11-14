# Portunus — TOML App Catalog (file recipes, DB for live data)

**Date:** 2026-11-13  
**Status:** design (audit round 1 applied)  
**Decision:** Approach A — directory of TOML recipes; on-demand install scripts for native CLIs (runner-executed).  
**Related:** `2026-11-09-portunus-design.md`, today’s `DEFAULT_APPS` / `catalog_apps` in `gateway/portunus_gateway/catalog.py`.  
**Audit:** `docs/audit/SYNTHESIS.md` (janus `*-flash` panel).

## 1. Problem

Well-known Connect recipes are seeded into SQLite (`catalog_apps` via `DEFAULT_APPS` + `ensure_defaults`). That mixes **code data** (OAuth URLs, connection type, skill paths) with **hot/live data** (connected servers, vault secrets, grants). Seeds also fight operator edits and require DB migrations for recipe changes.

## 2. Goals

1. Ship platform apps as **TOML files in the repo** (`catalog/apps/*.toml`).
2. Keep **only live MCP state** in the database: `servers`, vault, tools, grants, health, schema overrides.
3. Custom MCP = **direct server create** only (no operator catalog recipes; Catalog Config CRUD removed).
4. Native CLIs: **install scripts** (arch-aware, checksum-pinned), run **on demand on the runner** before first spawn — not multi-arch binaries in git.
5. Script-style skill trees under `third_party/` remain **deploy-guaranteed** on runners (verify at runtime, do not stream in v1).

## 3. Non-goals (v1)

- Operator-authored TOML overlays under `/etc/portunus/apps`.
- Control-plane blob sync of skill trees or installer scripts gateway→runner.
- Async install job + status API (synchronous wait before spawn only).
- `[artifacts.pins]` arch→URL table in TOML (pins stay inside install scripts for v1).
- Changing Omni meta-tools or agent key model.

## 4. Ownership split

| Data | Store |
|---|---|
| Recipe: slug, labels, connection, categories, tags, popular, sort_order, connection `spec` | TOML |
| Artifact declarations (`tree` verify / `install_script`) | TOML |
| Connected server after Connect (`slug`, argv/url/oauth snapshot, vault_version) | `servers` |
| Secrets / tokens | vault |
| Tools, schema overrides, grants, health | DB as today |

**Binding:** Connect sets `servers.slug` from the recipe slug (already true today). There is **no FK** to `catalog_apps`.

**Drift policy (normative):**

- **Spawn / skill-bridge inject / skill_auth / skill-bridge health** for a server whose `slug` exists in `AppCatalog` read the **current** TOML recipe (manifest, env_defaults used for inject).
- **Server row fields** created at Connect (`command`, `args`, `url`, `oauth`, vaulted secrets) stay as the **Connect-time snapshot** until the operator reconnects or edits the server.
- If a shipped slug is **removed** from TOML: existing servers keep running on their snapshot; skill-bridge inject that requires a live recipe returns a clear error (`recipe_missing`); Connect cards no longer show the app.
- Custom servers (slug not in `AppCatalog`): no artifact step; skill_auth uses vault/session on the server row only (never 422 solely for “unknown catalog slug”).

## 5. File layout

```text
catalog/apps/
  ardot.toml
  dropbox.toml
  …
deploy/installers/          # allowlisted install scripts
  install-rclone.sh
third_party/…               # skill trees (deploy-synced to runners)
packages/portunus_skill_bridge/manifests/*.yaml
```

Slug = filename stem unless `slug` is set. Duplicate slugs → **gateway refuses to start**.

## 6. TOML schema (normative)

```toml
slug = "quark"                  # optional; default = filename stem
label = "夸克网盘"
name = "夸克网盘"
description = "…"
connection = "skill_bridge"   # oauth | http | webdav | stdio | skill_bridge
popular = false
categories = ["Storage"]
tags = ["quark", "drive"]
sort_order = 76

[spec]
# Same shapes as today's validate_spec(connection, spec)
manifest = "quark.example.yaml"   # packaged skill-bridge manifest name
# command / args / env_defaults / secrets / oauth fields as today

[[artifacts]]
id = "quarkclouddrive"
kind = "tree"
path = "third_party/quarkclouddrive"          # deploy source (documentation + hash baseline)
install_as = "/var/lib/portunus/skills/quarkclouddrive"
# runtime: verify install_as exists (and optional tree digest); never copy in v1

[[artifacts]]
id = "rclone"
kind = "install_script"
script = "deploy/installers/install-rclone.sh"
script_sha256 = "<hex>"                       # required
install_as = "/var/lib/portunus/bin/rclone"
# script MUST: detect arch itself; fetch pinned URL+SHA256; atomic install;
# no-op if install_as hash already matches; write install_as.sha256 sidecar
```

- Invalid TOML / unknown `connection` / missing `script_sha256` for `install_script` / path escape → **gateway refuses to start**.
- `install_as` must stay under allowlisted prefixes: `/var/lib/portunus/bin/`, `/var/lib/portunus/skills/`, `/var/lib/portunus/skill-state/` (configurable list, default these three).
- `script` must resolve under `deploy/installers/` only (no `..`, no absolute paths outside).

## 7. Runtime: `AppCatalog`

In-memory `slug → Recipe` loaded at gateway start from `PORTUNUS_APPS_DIR` (default: packaged `catalog/apps`).

Replaces SQL `CatalogApp` reads:

1. `GET /api/apps` — TOML recipes; each item uses **`id` = slug** (string). SPA keys Connect cards by slug.
2. Connect — copy recipe → `POST /servers` with `slug` + vault secrets (unchanged spirit).
3. `registry` skill-bridge inject / `skill_auth` / skill-bridge health — `AppCatalog.get(server.slug)`.

`POST` / `PATCH` / `DELETE /api/apps` → **410 Gone** (no compat create path).

## 8. On-demand install lifecycle (normative)

**Who:** the **runner** executes install scripts (it owns the target filesystem). The gateway never shells out installers for remote runner sites.

**When:** immediately before first successful child **spawn** for that server if any artifact is unsatisfied. Connect may call the same ensure-path and wait (same code path) — not a second lifecycle.

**Steps:**

1. Resolve recipe by `server.slug`. If none → skip artifacts (custom server).
2. For each artifact in order:
   - **`tree`:** if `install_as` missing or fails digest check → fail with message pointing at deploy/provision (“skill tree not on runner”). No gateway→runner copy in v1.
   - **`install_script`:** runner verifies on-disk script bytes match `script_sha256` from the recipe payload the deploy placed on the runner; `flock`/`install_as.lock` (wait timeout 30s; stale lock → error); run as the runner’s MCP child uid (same privilege class as spawned MCP children, **not** root); env allowlist only (`PORTUNUS_ARTIFACT_ID`, `PORTUNUS_INSTALL_AS`, informational `PORTUNUS_ARCH`); cwd = empty scratch; timeout 120s; success requires `install_as` exists **and** `install_as.sha256` sidecar matches the installed object; stderr returned to gateway **redacted** (strip URLs with credentials, `Authorization`, tokens) and truncated.
3. Concurrent spawns share the lock; waiters re-check hash after lock.
4. Failure → `spawn_failed` (or Connect error); do not mark server healthy.

**Airgap:** if binary already present with matching sidecar → no network. If missing and script needs egress → fail fast with `artifact_install_failed` / clear “offline / egress denied” message (no long hang).

**Security:** scripts only from allowlisted dir shipped onto the runner with the platform; hash mismatch → refuse to execute; no operator-supplied script body in DB.

## 9. Migration

1. Add `catalog/apps/*.toml` for every current `DEFAULT_APPS` entry.
2. Stop `ensure_defaults` seeding / skill_bridge recipe refresh from SQL.
3. Existing `servers.slug` values already match recipe slugs for Connect-created apps — no new column required for binding.
4. Optional one-shot: export remaining `catalog_apps` rows to a dump file for operators who used Catalog Config CRUD; then **delete all `catalog_apps` rows**.
5. Drop `catalog_apps` table in a **later** migration after a bake period (deferred).
6. Dashboard: remove Catalog Config mutations; use slug as app id.

## 10. Dashboard / API contracts

- `GET /api/apps` → read-only TOML recipes (`id` = slug).
- Mutations → 410 Gone.
- Servers API unchanged; custom MCP still `POST /servers` without a catalog row.
- Connected tab: recipe match iff `server.slug` ∈ AppCatalog; else badge **Custom**.

## 11. Error contracts

| Case | Behavior |
|---|---|
| Invalid / duplicate TOML at startup | Gateway process exit |
| `recipe_missing` (slug removed from TOML, skill-bridge needs live recipe) | Invoke/skill_auth error with code `recipe_missing` |
| Tree missing / digest mismatch | `spawn_failed` — message names artifact id + deploy hint |
| Script hash mismatch | Refuse execute; `spawn_failed` |
| Lock wait timeout / stale lock | `spawn_failed` |
| Script timeout / nonzero / unsupported arch | `spawn_failed`; redacted stderr tail |
| Airgap + missing binary | `artifact_install_failed` (or `spawn_failed`) with offline hint |
| Custom server skill_auth | Uses server vault/session only |
| `/api/apps` mutations | 410 Gone |

## 12. Testing

- Load fixture TOML dir; validate_spec + list; duplicate slug → startup error.
- Install script: fake arch + fake download; lock concurrency; hash short-circuit; hash mismatch refuses execute.
- Tree verify-only failure when `install_as` absent.
- Registry inject/skill_auth without `catalog_apps` rows; custom server skill_auth without recipe.
- Drift: change TOML manifest pointer → next inject sees new manifest; server argv unchanged.
- API 410 on app mutations; GET apps id === slug.

## 13. Deferred

- Pre-warm installers at deploy time.
- Host overlay recipes; control-plane blob sync; async install status API.
- `[artifacts.pins]` in TOML; drop `catalog_apps` table.
