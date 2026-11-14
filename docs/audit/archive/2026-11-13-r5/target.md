# Portunus — TOML App Catalog (file recipes, DB for live data)

**Date:** 2026-11-13  
**Status:** design (audit round 4 applied)  
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

- **Binding:** Connect from a catalog card sets `servers.recipe_slug` = recipe slug (immutable after create). Custom create leaves `recipe_slug` NULL. Artifact ensure and live recipe lookup use **`recipe_slug`** only.
- **Live from TOML (when `recipe_slug` is set and ∈ AppCatalog):** skill-bridge **manifest YAML is read at inject time** from the **gateway’s** packaged manifests dir (runners never read TOML/manifests for inject — gateway injects env before spawn); **non-secret `env_defaults`** at inject.
- **Frozen on the server row (Connect snapshot):** `command`, `args`, `url`, `oauth` public fields, **vaulted secrets**, and an **`artifact_snapshot`** JSON copy of each `[[artifacts]]` row (`id`, `kind`, `script`, `script_sha256`, `install_as`, `content_sha256` / `content_by_arch`). Ensure + argv paths for installed bins use **this snapshot**, not live TOML. Changing artifact paths/hashes in TOML affects **new** Connects only; existing servers need reconnect to refresh the snapshot.
- **`env_defaults` must not contain literal secrets** — only key names / non-secret defaults; `validate_spec` rejects secret-looking values in that map.
- If `recipe_slug` is set but **missing** from TOML: Connect cards disappear; existing servers keep snapshot + may still ensure from snapshot; skill-bridge inject that needs a live recipe returns `recipe_missing`; dashboard **Orphaned recipe**.
- Custom (`recipe_slug` NULL): no artifact step; skill_auth uses vault/session only. Dashboard **Custom**.
- **`servers.slug` vs `recipe_slug`:** for catalog Connect they start equal; `slug` remains the operator-visible server key and may later diverge only via explicit rename APIs if added. Join key for recipes is always `recipe_slug`.

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
script = "install-rclone.sh"                  # path relative to deploy/installers/ only
script_sha256 = "<hex>"                       # sha256 of that script file’s bytes
install_as = "/var/lib/portunus/bin/rclone"
# Exactly ONE of:
content_sha256 = "<hex>"                      # single-arch recipes only
# OR:
# [artifacts.content_by_arch]
# "linux/amd64" = "<hex>"
# "linux/arm64" = "<hex>"
# Keys = GOOS/GOARCH. Startup fails if both forms present, if neither present,
# or if content_by_arch is empty. Multi-arch install scripts MUST use content_by_arch.
# script MUST: detect arch; fetch its own pinned URL+SHA256; atomic rename of a
# single file into install_as (directories / multi-file unpack → deferred kind);
# write sidecar install_as.sha256 as one line of lowercase hex (no filename suffix).
```

- Invalid TOML / unknown `connection` / bad hash XOR / path escape → **gateway refuses to start**.
- `install_as` must stay under allowlisted prefixes: `/var/lib/portunus/bin/`, `/var/lib/portunus/skills/` (configurable; default these two).
- `script` is relative to `deploy/installers/` only (no `..`, no absolute paths). RPC field `script_relpath` is that same relative string.
- `label` = Connect-card title; `name` = optional display alias (may equal `label`).
- Dev/ops: `portunus catalog verify` (or equivalent CI) recomputes `script_sha256` from disk; `content_*` pins are updated when releasing a new binary.

## 7. Runtime: `AppCatalog`

In-memory `slug → Recipe` loaded at gateway **startup** from `PORTUNUS_APPS_DIR`. If the env var is **unset**, use the packaged `catalog/apps`. If the env var is **set**, it **replaces** the packaged set (no merge); an explicitly set path that is missing, unreadable, or contains zero recipes → **process exit**. TOML recipe metadata is startup-immutable (SIGHUP reload deferred); only manifest YAML bodies are re-read from disk at inject.

Replaces SQL `CatalogApp` reads:

1. `GET /api/apps` — TOML recipes; each item uses **`id` = slug** (string). SPA keys Connect cards by slug.
2. Connect — gateway resolves the recipe **server-side** by slug; persists `slug`, **`recipe_slug`**, vault secrets, and **`artifact_snapshot`**. Client cannot set `recipe_slug` arbitrarily; custom create omits it and must not use a slug present in `AppCatalog` (reserved). Adding a new TOML slug that collides with an existing custom server’s `slug` is a **deploy-time ops conflict** — rename the custom server before shipping that recipe.
3. `registry` skill-bridge inject / `skill_auth` / skill-bridge health — `AppCatalog.get(server.recipe_slug)` when set (gateway-side).

`POST` / `PATCH` / `DELETE /api/apps` → **410 Gone**. Ship the SPA that stops those mutations **in the same release** as the 410.

## 8. On-demand install lifecycle (normative)

### 8.1 Distribution (ops, not Connect-time download of scripts)

Deploy **must** place onto every runner (same channel as today’s skill_bridge sync — rsync/image layer; **no script-bytes blob** in the RPC):

- `deploy/installers/*.sh` (bit-identical to the gateway’s reviewed tree)
- skill `tree` directories under `/var/lib/portunus/skills/…` when those artifacts are declared
- a **runner artifact pin map** derived from the same reviewed recipes (artifact_id → script_relpath, script_sha256, allowed install_as prefix). Built at package/deploy time from TOML — not invented by the gateway at RPC time.

Runner **site registration** (or ensure handshake) advertises `GOOS`/`GOARCH` so the gateway can select `content_by_arch[GOOS/GOARCH]` when present.

At ensure time the gateway sends **`ensure_artifact`** on the **existing authenticated runner control channel** (same trust as spawn — mTLS or runner token). Fields required by kind:

```text
# install_script
{ artifact_id, kind: "install_script", script_relpath, script_sha256, content_sha256, install_as }
# tree
{ artifact_id, kind: "tree", install_as }
```

`content_sha256` in the RPC is either the flat pin or the entry for the runner’s advertised `GOOS/GOARCH`.

**Trust root:** deploy-published files + pin map on the runner. The runner **independently**:

1. Looks up `artifact_id` in its local pin map; refuse if unknown (`artifact_path_rejected`).
2. Requires RPC `script_relpath` / `script_sha256` / `install_as` to match the pin map (normalized realpath; no `..`; `install_as` under allowlisted prefixes; no symlink escape).
3. Refuses exec unless `sha256(local script) == script_sha256`.
4. Never fetches script bytes from the gateway.

A compromised gateway can only request artifacts **already listed in the deploy pin map**, not arbitrary paths. Signed RPCs remain deferred.

### 8.2 Privilege

- Install executes as a **dedicated installer identity** (e.g. runner service uid / `portunus-install`), **not** the MCP child uid.
- `/var/lib/portunus/bin` and `/var/lib/portunus/skills` are writable by that installer identity and **not writable** by MCP children (dirs `0755` or tighter; artifacts owned by installer, mode `0555`/`0755` as appropriate).
- MCP children may only read/execute installed artifacts.

### 8.3 When / steps

**When:** immediately before child **spawn** on a **specific runner site** if any snapshot artifact is unsatisfied **on that site** (per-site cache of last good ensure is allowed; reschedule to another site re-ensures). Connect may invoke the same path against a chosen site and wait. Concurrent Connect/spawn during a 120s install: flock wait is ≤30s — waiters can fail with lock timeout while another ensure runs (documented; async install deferred).

1. If `server.recipe_slug` is NULL → skip artifacts. If set: use **`artifact_snapshot`** on the server row (not live TOML). If snapshot empty and recipe missing → `recipe_missing` for skill-bridge; do not invent customs.
2. Gateway picks `content_sha256` from snapshot flat pin or `content_by_arch[runner GOOS/GOARCH]`; unknown arch → `artifact_unsupported_arch` without running the script.
3. Gateway issues `ensure_artifact` per snapshot artifact (in order) to the **spawn-target** site.
4. Runner:
   - **`tree`:** existence-only check on `install_as` (never hash a directory in v1; content drift undetectable — ops must deploy the right tree). Failure → deploy hint.
   - **`install_script`:** pin-map + path checks (§8.1); `flock` on `install_as.lock` under the installer-writable dir (wait ≤30s; no “stale lock” — kernel releases on death); **no-op** if file exists and `sha256(install_as) == content_sha256` and sidecar line matches; if sidecar **missing** on an existing file → treat as unsatisfied (re-run or fail closed, no silent trust); else run as installer uid with `PORTUNUS_ARTIFACT_ID`, `PORTUNUS_INSTALL_AS`, plus minimal env (`PATH`, `HOME`, `TMPDIR`, `LANG`, optional CA/proxy); cwd = empty scratch; **timeout 120s kills the process group** (SIGTERM then SIGKILL); atomic rename of one file into `install_as`; success requires hash == `content_sha256` then write sidecar; on failure return `{artifact_id, exit_code, duration_ms, stderr_tail}` where `stderr_tail` is last 4KiB or 100 lines (whichever smaller); exit **78** = unsupported arch inside script (mapped to `artifact_unsupported_arch`).
5. Waiters re-verify after lock; failure codes: `artifact_script_missing`, `artifact_hash_mismatch`, `artifact_path_rejected`, `artifact_install_failed`, `artifact_unsupported_arch`, `runner_unreachable`.

**Airgap:** content hash already satisfied → no network. Else script may need egress within 120s or fail.

**Provenance:** snapshot pins + deploy pin map + local script bytes. URL honesty lives inside reviewed scripts.

## 9. Migration

Split across releases:

**Release A — schema + TOML read path (SQL still served if needed)**  
1. Add `catalog/apps/*.toml` for every current `DEFAULT_APPS` entry.  
2. Add nullable `servers.recipe_slug` + `artifact_snapshot` (JSON, nullable).  
3. **Pre-migration audit:** dump `DISTINCT servers.slug`; classify each as (a) matches a TOML `slug` field value, (b) custom, (c) unknown/operator CRUD. Require an explicit map file for (c); **fail the migration job** if unmapped. Never bind a custom server solely because its slug string equals a recipe stem.  
4. Backfill `recipe_slug` + snapshot **only** for (a) and mapped (c→catalog). Leave true customs NULL.  
5. **Mandatory** export of remaining operator `catalog_apps` rows to a dump before any delete.

**Release B — cutover**  
6. Stop `ensure_defaults` / SQL catalog reads; SPA + API use TOML; `/api/apps` mutations → 410.  
7. Delete `catalog_apps` rows only after export + bake; drop table in a **later** migration (deferred).

## 10. Dashboard / API contracts

- `GET /api/apps` → read-only TOML recipes (`id` = slug).
- Mutations → 410 Gone.
- Servers API: custom MCP still `POST /servers` with `recipe_slug` unset; catalog Connect sets `recipe_slug`.
- Connected tab: `recipe_slug ∈ AppCatalog` → normal; `recipe_slug` set but missing → **Orphaned recipe**; `recipe_slug` NULL → **Custom**.

## 11. Error contracts

| Case | Behavior |
|---|---|
| Invalid / duplicate TOML / bad content hash XOR at startup | Gateway process exit |
| Explicit `PORTUNUS_APPS_DIR` missing/empty/unreadable | Gateway process exit |
| `recipe_missing` | Invoke/skill_auth error when live recipe required and absent |
| `manifest_missing` | Invoke/skill_auth error |
| Tree path absent | `spawn_failed` + deploy hint |
| Pin-map / path rejection | `artifact_path_rejected` |
| Script missing / `script_sha256` mismatch | `artifact_script_missing` / `artifact_hash_mismatch` |
| Installed object hash ≠ pin | `artifact_install_failed` |
| Unsupported arch (gateway or exit 78) | `artifact_unsupported_arch` |
| Lock wait timeout | `spawn_failed` |
| Script timeout / nonzero | `artifact_install_failed` + stderr_tail |
| Runner control channel down | `runner_unreachable` |
| Custom skill_auth | Vault/session only |
| `/api/apps` mutations | 410 Gone |
| Custom create with reserved slug | 400 |
| Migration unmapped slug | Migration job abort |

## 12. Testing

- Load fixture TOML dir; validate_spec + list; duplicate slug → startup error.
- Install script: fake arch + fake download; lock concurrency; hash short-circuit; hash mismatch refuses execute.
- Tree verify-only failure when `install_as` absent.
- Registry inject/skill_auth without `catalog_apps` rows; custom server skill_auth without recipe.
- Drift: change TOML manifest pointer → next inject sees new manifest; server argv unchanged.
- API 410 on app mutations; GET apps id === slug; reserved-slug rejection on custom create.
- `content_sha256` short-circuit; mismatch refuses success.

## 13. Deferred

- Pre-warm installers; async install + longer lock wait; Connect readiness UI.
- Host overlay recipes; control-plane script-byte sync; signed ensure RPCs.
- Full `[artifacts.pins]` URL table in TOML; multi-file / directory install kind.
- Drop `catalog_apps` table; detach-Orphaned-to-Custom; `recipe_rev`; SIGHUP catalog reload.
