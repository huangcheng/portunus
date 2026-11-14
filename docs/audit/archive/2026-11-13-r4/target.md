# Portunus — TOML App Catalog (file recipes, DB for live data)

**Date:** 2026-11-13  
**Status:** design (audit round 3 applied)  
**Decision:** Approach A — directory of TOML recipes; on-demand install scripts for native CLIs (runner-executed).  
**Related:** `2026-11-09-portunus-design.md`, today’s `DEFAULT_APPS` / `catalog_apps` in `gateway/portunus_gateway/catalog.py`.  
**Audit:** `docs/audit/` (janus `*-flash` panel; see archive + SYNTHESIS).

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

- **Binding:** Connect from a catalog card sets `servers.recipe_slug` = recipe slug (immutable after create). Custom create leaves `recipe_slug` NULL. Artifact ensure and live recipe lookup use **`recipe_slug`**, not a free-form equality on `servers.slug` alone.
- **Live from TOML (when `recipe_slug` is set and ∈ AppCatalog):** skill-bridge **manifest YAML is read at inject time** from the packaged manifests dir named by the current recipe (not cached for the life of the process beyond normal FS reads); **non-secret `env_defaults`** at inject; artifact list / hashes for ensure-artifact.
- **Frozen on the server row (Connect snapshot):** `command`, `args`, `url`, `oauth` public fields, **vaulted secrets**, and the **`install_as` paths** copied into argv at Connect. Operator must edit/reconnect to change those.
- **`env_defaults` must not contain literal secrets** — only key names / non-secret defaults; `validate_spec` rejects secret-looking values in that map.
- If `recipe_slug` is set but **missing** from TOML: Connect cards for that slug disappear; existing servers keep snapshot; skill-bridge inject returns `recipe_missing`; dashboard label **Orphaned recipe**.
- Custom (`recipe_slug` NULL): no artifact step; skill_auth uses vault/session on the server row only. Dashboard **Custom**.

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
path = "third_party/quarkclouddrive"          # deploy source (ops docs); not hashed in v1
install_as = "/var/lib/portunus/skills/quarkclouddrive"
# runtime: existence-only verify of install_as; never copy in v1

[[artifacts]]
id = "rclone"
kind = "install_script"
script = "deploy/installers/install-rclone.sh"
script_sha256 = "<hex>"                       # required — sha256 of the script file bytes
install_as = "/var/lib/portunus/bin/rclone"
content_sha256 = "<hex>"                      # required — sha256 of install_as after a good install
# Multi-arch: script detects arch; embeds per-arch URL+SHA256; after install the
# resulting install_as MUST hash to content_sha256 for that arch build line, OR the
# recipe ships one content_sha256 per supported arch via [artifacts.content_by_arch]
# (optional table; if present, RPC sends the entry for the runner's GOARCH/GOOS).
# script MUST: atomic rename into install_as; write install_as.sha256 sidecar = content hash.
```

- Invalid TOML / unknown `connection` / missing `script_sha256` or `content_sha256` for `install_script` / path escape → **gateway refuses to start**.
- `install_as` must stay under allowlisted prefixes: `/var/lib/portunus/bin/`, `/var/lib/portunus/skills/` (configurable; default these two — not `skill-state/` unless a future artifact kind needs it).
- `script` must resolve under `deploy/installers/` only (no `..`, no absolute paths outside).
- `label` is the Connect-card title; `name` is optional display alias (may equal `label`).

## 7. Runtime: `AppCatalog`

In-memory `slug → Recipe` loaded at gateway start from `PORTUNUS_APPS_DIR` (default: packaged `catalog/apps`).

Replaces SQL `CatalogApp` reads:

1. `GET /api/apps` — TOML recipes; each item uses **`id` = slug** (string). SPA keys Connect cards by slug.
2. Connect — gateway resolves the recipe **server-side** by slug; `POST /servers` sets `slug` + **`recipe_slug`** from that recipe and vaults secrets. Client-supplied `slug`/`recipe_slug` that are not in `AppCatalog` are rejected for catalog Connect; custom create omits `recipe_slug` and must not use a slug currently present in `AppCatalog` (reserved).
3. `registry` skill-bridge inject / `skill_auth` / skill-bridge health — `AppCatalog.get(server.recipe_slug)` when set.

`POST` / `PATCH` / `DELETE /api/apps` → **410 Gone** (no compat create path). Ship the SPA that stops calling those mutations **in the same release** as the 410 (avoid mixed-version Connect cards during rollout).

## 8. On-demand install lifecycle (normative)

### 8.1 Distribution (ops, not Connect-time download of scripts)

Deploy **must** place onto every runner (same channel as today’s skill_bridge sync — rsync/image layer; **no script-bytes blob** in the RPC):

- `deploy/installers/*.sh` (bit-identical to the gateway’s reviewed tree)
- skill `tree` directories under `/var/lib/portunus/skills/…` when those artifacts are declared

Gateway at startup loads TOML including `script_sha256` / `content_sha256`. At ensure time it sends the runner an **`ensure_artifact`** RPC on the **existing authenticated runner control channel** (same trust as spawn / other runner RPCs — mTLS or runner token as deployed today):

```text
{ artifact_id, kind, script_relpath?, script_sha256?, content_sha256?, install_as }
```

**Trust root:** deploy-published files on the runner disk. The runner (a) refuses if the script path is outside the allowlisted installers dir or the file is missing, (b) refuses exec unless `sha256(local script) == script_sha256` from the RPC, (c) does not fetch script bytes from the gateway. A compromised gateway can only request execution of an **already-deployed** script whose hash it knows — not push new bytes in v1. (Runner-local pin allowlists / signed RPCs are deferred.)

### 8.2 Privilege

- Install executes as a **dedicated installer identity** (e.g. runner service uid / `portunus-install`), **not** the MCP child uid.
- `/var/lib/portunus/bin` and `/var/lib/portunus/skills` are writable by that installer identity and **not writable** by MCP children (dirs `0755` or tighter; artifacts owned by installer, mode `0555`/`0755` as appropriate).
- MCP children may only read/execute installed artifacts.

### 8.3 When / steps

**When:** immediately before first successful child **spawn** on a **specific runner site** if any artifact is unsatisfied **on that site**. Connect may invoke the same ensure path against the intended site and wait (one code path). Satisfaction is **per runner site**, not global — reschedule to another site re-ensures.

1. If `server.recipe_slug` is NULL → skip artifacts (custom). If set but not in `AppCatalog` → do not run installers; surface `recipe_missing` when inject/ensure needs the recipe (do not treat as custom for skill-bridge).
2. Gateway issues `ensure_artifact` per artifact (in order) to the **spawn-target** runner site.
3. Runner:
   - **`tree`:** verify `install_as` exists (existence-only). On failure → error “deploy skill tree first” (no copy).
   - **`install_script`:** resolve `script_relpath` under allowlisted installers dir; **refuse** if missing or `sha256(file) != script_sha256`; take exclusive `flock` on `install_as.lock` (wait ≤30s; on wait timeout → error — flock is released by the kernel when the holder dies, so there is no separate “stale lock” state on local disk); **no-op success** if `install_as` exists and `sha256(install_as) == content_sha256` from RPC and sidecar agrees; else run script as installer uid with env: `PORTUNUS_ARTIFACT_ID`, `PORTUNUS_INSTALL_AS`, plus a minimal process env (`PATH`, `HOME`, `TMPDIR`, `LANG`, and CA/proxy vars if configured on the runner) — not a fully open environment; cwd = empty scratch; timeout 120s; script must atomic-rename into `install_as`; on success require `sha256(install_as) == content_sha256` and write matching sidecar; return `{artifact_id, exit_code, duration_ms, stderr_tail}` (stdout discarded) on failure.
4. Concurrent ensures share the lock; waiters re-verify `content_sha256` after.
5. Failure → `spawn_failed` / Connect error with distinct causes where practical (`artifact_script_missing`, `artifact_hash_mismatch`, `artifact_install_failed`, `runner_unreachable`); server not marked healthy.

**Airgap:** `content_sha256` already satisfied → no network. If not satisfied, the script may need egress; ops that forbid egress must pre-install or accept `artifact_install_failed` within the 120s script timeout (airgap is the content-hash short-circuit, not magic offline DNS detection).

**Provenance:** `script_sha256` + `content_sha256` in TOML are the reviewed pins; deploy publishes matching script bytes; runner enforces both. URL honesty for downloads lives in the **reviewed script contents** (per-arch URL + SHA256 inside the script).

## 9. Migration

1. Add `catalog/apps/*.toml` for every current `DEFAULT_APPS` entry.
2. Stop `ensure_defaults` seeding / skill_bridge recipe refresh from SQL.
3. Migration: add nullable `servers.recipe_slug`; backfill `recipe_slug = slug` where `slug` matches a shipped recipe stem after TOML lands; leave NULL for true customs.
4. Optional one-shot: export remaining `catalog_apps` rows to a dump file for operators who used Catalog Config CRUD; then **delete all `catalog_apps` rows** (only after export; do not delete while SPA still reads SQL).
5. Drop `catalog_apps` table in a **later** migration after a bake period (deferred).
6. Dashboard: remove Catalog Config mutations in the **same** release as API 410; use slug as app `id` (document SPA break: integer ids → string slugs).

## 10. Dashboard / API contracts

- `GET /api/apps` → read-only TOML recipes (`id` = slug).
- Mutations → 410 Gone.
- Servers API: custom MCP still `POST /servers` with `recipe_slug` unset; catalog Connect sets `recipe_slug`.
- Connected tab: `recipe_slug ∈ AppCatalog` → normal; `recipe_slug` set but missing → **Orphaned recipe**; `recipe_slug` NULL → **Custom**.

## 11. Error contracts

| Case | Behavior |
|---|---|
| Invalid / duplicate TOML / missing required hashes at startup | Gateway process exit |
| Empty / missing `PORTUNUS_APPS_DIR` | Gateway process exit |
| `recipe_missing` (`recipe_slug` set but not in TOML) | Invoke/skill_auth/ensure error `recipe_missing` |
| `manifest_missing` (recipe points at absent manifest file) | Invoke/skill_auth error `manifest_missing` |
| Tree path absent on runner | `spawn_failed` — artifact id + deploy hint |
| Script missing on runner / `script_sha256` mismatch | Refuse execute; `artifact_hash_mismatch` / `artifact_script_missing` |
| Installed object hash ≠ `content_sha256` | `artifact_install_failed` |
| Lock wait timeout | `spawn_failed` |
| Script timeout / nonzero / unsupported arch | `artifact_install_failed`; stderr_tail |
| Runner control channel down | `runner_unreachable` |
| Custom server skill_auth | Uses server vault/session only |
| `/api/apps` mutations | 410 Gone |
| Custom create with reserved (catalog) slug | 400 |

## 12. Testing

- Load fixture TOML dir; validate_spec + list; duplicate slug → startup error.
- Install script: fake arch + fake download; lock concurrency; hash short-circuit; hash mismatch refuses execute.
- Tree verify-only failure when `install_as` absent.
- Registry inject/skill_auth without `catalog_apps` rows; custom server skill_auth without recipe.
- Drift: change TOML manifest pointer → next inject sees new manifest; server argv unchanged.
- API 410 on app mutations; GET apps id === slug; reserved-slug rejection on custom create.
- `content_sha256` short-circuit; mismatch refuses success.

## 13. Deferred

- Pre-warm installers at deploy time.
- Host overlay recipes; control-plane script-byte sync; async install status API; signed ensure RPCs / runner-local pin allowlists.
- Full `[artifacts.pins]` URL table in TOML (scripts keep URL pins internally; TOML already requires `content_sha256`).
- Drop `catalog_apps` table; detach-Orphaned-to-Custom endpoint; recipe revision column.
