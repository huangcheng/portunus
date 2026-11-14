# Portunus — TOML App Catalog (file recipes, DB for live data)

**Date:** 2026-11-13  
**Status:** design  
**Decision:** Approach A — directory of TOML recipes; on-demand install scripts for native CLIs.  
**Related:** `2026-11-09-portunus-design.md`, catalog/`DEFAULT_APPS` in `gateway/portunus_gateway/catalog.py`.

## 1. Problem

Well-known Connect recipes are seeded into SQLite (`catalog_apps` via `DEFAULT_APPS` + `ensure_defaults`). That mixes **code data** (OAuth URLs, connection type, skill paths) with **hot/live data** (connected servers, vault secrets, grants). Seeds also fight operator edits and require DB migrations for recipe changes.

## 2. Goals

1. Ship platform apps as **TOML files in the repo** (`catalog/apps/*.toml`).
2. Keep **only live MCP state** in the database: `servers`, vault, tools, grants, health, schema overrides.
3. Custom MCP = **direct server create** only (no operator catalog recipes; Catalog Config CRUD goes away).
4. Vendored native CLIs: **install scripts** (arch-aware, checksum-pinned), run **on demand** on first Connect/spawn — not multi-arch binaries in git.
5. Skill-bridge / Quark / Guangyapan script trees remain copyable trees under `third_party/` (arch-agnostic).

## 3. Non-goals (v1)

- Operator-authored TOML overlays under `/etc/portunus/apps`.
- On-demand download of arbitrary upstream MCP servers from the public internet beyond script-declared pinned URLs.
- Replacing skill-bridge YAML manifests (TOML **references** them).
- Changing Omni meta-tool surface or agent key model.

## 4. Ownership split

| Data | Store |
|---|---|
| Recipe: slug, labels, connection, categories, tags, popular, sort_order, connection `spec` | TOML |
| Artifact declarations (`tree` / `install_script`) | TOML |
| Connected server argv/url/oauth config after Connect | `servers` |
| Secrets / tokens | vault |
| Discovered tools, schema overrides, grants, health | DB as today |

## 5. File layout

```text
catalog/apps/
  ardot.toml
  dropbox.toml
  nutstore.toml
  …
deploy/installers/          # install scripts referenced by TOML
  install-rclone.sh
  …
third_party/…               # skill trees (unchanged role)
packages/portunus_skill_bridge/manifests/*.yaml
```

Slug = filename stem unless `slug` is set. Duplicate slugs across files → startup fail.

## 6. TOML schema (normative)

```toml
slug = "quark"                    # optional; default = filename stem
label = "夸克网盘"
name = "夸克网盘"
description = "…"
connection = "skill_bridge"     # oauth | http | webdav | stdio | skill_bridge
popular = false
categories = ["Storage"]
tags = ["quark", "drive"]
sort_order = 76

[spec]
# connection-specific fields — same shapes as today's DEFAULT_APPS / validate_spec
# skill_bridge example:
manifest = "quark.example.yaml"   # resolved under packaged manifests/
command = ["python", "-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"]
# env_defaults, secrets defs as today

[[artifacts]]
id = "quarkclouddrive"
kind = "tree"
path = "third_party/quarkclouddrive"
install_as = "/var/lib/portunus/skills/quarkclouddrive"

[[artifacts]]
id = "rclone"
kind = "install_script"
script = "deploy/installers/install-rclone.sh"
install_as = "/var/lib/portunus/bin/rclone"
# script MUST: detect arch, fetch pinned URL+SHA256, atomic install, no-op if hash matches
```

Validation: gateway loads TOML at startup with the same `validate_spec(connection, spec)` rules as today. Unknown `connection` / invalid artifact → refuse to start (or skip file with hard error in prod — **refuse to start**).

## 7. Runtime: `AppCatalog`

In-memory index `slug → Recipe` loaded once at gateway start from `PORTUNUS_APPS_DIR` (default: packaged/`catalog/apps` next to the install, overridable).

Replaces SQL reads that today hit `CatalogApp`:

1. `GET /api/apps` — list recipes from `AppCatalog` (synthetic `id` = slug string for SPA compatibility, or keep UUID-less and switch SPA to slug keys).
2. Connect panels — unchanged flow: copy recipe → `POST /servers` + vault secrets.
3. `registry._inject_skill_bridge_manifest` / `skill_auth` / skill-bridge health classification — resolve by slug via `AppCatalog`, not `catalog_apps`.

No `POST/PATCH/DELETE /api/apps` for v1 (404 or gone). Catalog Config edit UI removed; Apps page still shows shipped recipes as Connect cards.

## 8. On-demand install lifecycle

Trigger: before first successful child start for a server whose recipe lists `artifacts`, or at Connect when the dashboard/ops path can wait.

1. Gateway (or runner helper) evaluates each artifact in order.
2. `kind = tree`: if `install_as` missing or mtime/hash stale vs source tree, sync tree (rsync-like copy from image/bundle path available to the runner). Prefer runner-local copy from the gateway-bundled payload the deploy already syncs; if the tree is only on the gateway host, **v1 requires the skill tree to be present on the runner image/volume** (same as today) — Connect does not stream multi‑MB trees over the control plane in v1 unless we add an explicit `content_addressed` sync op (out of scope). Practical v1: `tree` artifacts are ensured by deploy **or** by an install script that fetches a pinned tarball; pure “copy from gateway git checkout” is deploy-time.
3. `kind = install_script`: runner executes the script under a lock (`flock` on `install_as.lock`), with env `PORTUNUS_ARTIFACT_ID`, `PORTUNUS_INSTALL_AS`, `PORTUNUS_ARCH`. Script is read from the **allowlisted path** shipped with the platform (never from DB). Timeout budget (e.g. 120s). Success = `install_as` exists and optional `.sha256` sidecar matches.
4. Concurrent Connect/spawn: one installer wins; others wait on the lock.
5. Failure → `spawn_failed` / Connect error with script stderr tail (truncated); do not mark server healthy.

**Security:** only scripts shipped under `deploy/installers/` (or a configured allowlist dir) may run; path traversal rejected; no operator-supplied script body.

## 9. Migration

1. Ship `catalog/apps/*.toml` equivalent to current `DEFAULT_APPS`.
2. Gateway stop seeding `ensure_defaults` for well-known apps.
3. Alembic/data: delete `catalog_apps` rows (all of them if no custom recipes are supported). Optionally drop `catalog_apps` table in a follow-up migration once API CRUD is removed.
4. Existing `servers` rows keep working; skill-bridge inject switches to TOML by slug.
5. Dashboard: remove Catalog Config mutations; key Connect cards by slug.

## 10. Dashboard / API contracts

- `GET /api/apps` → TOML recipes (read-only).
- `POST/PATCH/DELETE /api/apps` → removed.
- `GET/POST /api/servers` → unchanged; custom MCP still creates servers without a catalog row.
- Join Connected tab: `server.slug` ∈ AppCatalog or “custom” badge if no recipe.

## 11. Error contracts

| Case | Behavior |
|---|---|
| Invalid TOML / duplicate slug | Gateway process exit on startup |
| Missing `install_as` after script | Connect/spawn fails; message names artifact id |
| Script timeout / nonzero exit | Same; include truncated stderr |
| Unknown slug for skill_auth | 422 as today |
| Custom server (no TOML) | No artifacts step; spawn uses server row only |

## 12. Testing

- Load fixture TOML dir; assert validate_spec + list.
- Duplicate slug / bad connection → startup error.
- Install script: fake arch + fake download endpoint; lock concurrency; hash short-circuit.
- Registry inject/skill_auth without `catalog_apps` rows.
- Migration empties catalog_apps; Connect still creates servers from TOML.

## 13. Open follow-ups (explicitly deferred)

- Pre-warm installers during deploy (option 3 hybrid).
- Host overlay dir for private recipes.
- Streaming `tree` sync gateway→runner for airgapped skill updates without rebuild.
- Dropping `catalog_apps` table after a bake period.
