# Portunus — TOML App Catalog (file recipes, DB for live data)

**Date:** 2026-11-13  
**Status:** design (audit-passed: 0 NO-GO on rounds 5–6; residual splits deferred)  
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
5. Script-style skill trees under `third_party/` are **deploy-placed** on runners; v1 runtime check is **existence-only** (content integrity is an ops/deploy concern, not hashed in-gateway).

## 3. Non-goals (v1)

- Operator-authored TOML **overlays** under `/etc/portunus/apps` (merge). `PORTUNUS_APPS_DIR` may **replace** the packaged dir for staging/mirrors — not a merge overlay.
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
- **Frozen on the server row (Connect snapshot):** `command`, `args`, `url`, `oauth` public fields, **vaulted secrets** (secret *values* live in the vault; the row holds vault references / versions), and an **`artifact_snapshot`** JSON array with **per-kind** shapes:
  - `install_script`: `{id, kind, script, script_sha256, install_as, content_sha256 | content_by_arch}`
  - `tree`: `{id, kind, path, install_as}` only  
  Ensure + argv paths for installed bins use **this snapshot**, not live TOML. Pin-map updates must **retain historical** `(script_sha256, content_sha256, install_as)` tuples so old snapshots keep ensuring without reconnect; operators may still reconnect to pick up new pins.
- **`env_defaults` must not contain literal secrets** — values may only be empty, booleans, or non-secret literals that do **not** match vault secret *names* declared in the same spec; secret material belongs in `secrets` / vault only. `validate_spec` enforces that mechanically (no heuristic “looks like a password”).
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
# Single-arch form (do not also set content_by_arch):
arch = "linux/amd64"
content_sha256 = "<hex>"
# Multi-arch form instead (omit arch= and content_sha256):
# [artifacts.content_by_arch]
# "linux/amd64" = "<hex>"
# "linux/arm64" = "<hex>"
# XOR enforced at startup for install_script; trees omit all content_* / arch.
# Authority: TOML content_* is what the runner checks after install. The script MUST
# verify its download against its embedded URL pins, then leave install_as hashing to
# content_*. Disagreement → artifact_install_failed (not a silent pass).
# script MUST: detect arch; fetch pinned URL; verify download; atomic rename of a
# single file into install_as from a scratch dir on the **same filesystem** as
# install_as (avoid EXDEV); directories / multi-file unpack → deferred kind.
# Runner (not the script) writes sidecar install_as.sha256 (one line lowercase hex)
# after verifying sha256(install_as) == content_sha256, under the same flock.
```

- Invalid TOML / unknown `connection` / bad hash XOR (install_script) / path escape / **duplicate `install_as` across recipes** / duplicate `(recipe_slug, artifact id)` → **gateway refuses to start**.
- `install_as` must stay under allowlisted prefixes: `/var/lib/portunus/bin/`, `/var/lib/portunus/skills/` (configurable; default these two).
- `script` is relative to `deploy/installers/` only (no `..`, no absolute paths). RPC field `script_relpath` is that same relative string.
- Pin-map / ensure key is **`(recipe_slug, artifact_id)`** end-to-end (RPC carries both).
- `label` = Connect-card title; `name` = optional display alias (may equal `label`).
- Runners are **Linux** in v1 (`.sh` + GOOS/GOARCH). Dev/ops: `portunus catalog verify` recomputes `script_sha256`; CI must fail on pin drift.

## 7. Runtime: `AppCatalog`

In-memory `slug → Recipe` loaded at gateway **startup** from `PORTUNUS_APPS_DIR`. If the env var is **unset**, use the packaged `catalog/apps`. If the env var is **set**, it **replaces** the packaged set (no merge); an explicitly set path that is missing, unreadable, or contains zero recipes → **process exit**. Skill-bridge **manifest files** always resolve from the packaged manifests directory (not from `PORTUNUS_APPS_DIR`). TOML recipe records (including the manifest *filename* pointer) are startup-immutable; changing the pointer requires gateway restart. The manifest **YAML body** at that filename is re-read from disk at each inject.

Replaces SQL `CatalogApp` reads:

1. `GET /api/apps` — TOML recipes; each item uses **`id` = slug** (string). SPA keys Connect cards by slug.
2. Connect — gateway resolves the recipe **server-side** by slug; persists `slug`, **`recipe_slug`**, vault secrets, and **`artifact_snapshot`**. Invariant: `recipe_slug IS NULL` ⇔ `artifact_snapshot` is null/empty. Client cannot set `recipe_slug` arbitrarily; custom create omits it and must not use a slug present in `AppCatalog` (reserved). Gateway **startup fails** if any TOML slug equals an existing `servers.slug` where `recipe_slug` IS NULL (custom/reserved collision) — no silent remedy.
3. `registry` skill-bridge inject / `skill_auth` / skill-bridge health — `AppCatalog.get(server.recipe_slug)` when set (gateway-side).

`POST` / `PATCH` / `DELETE /api/apps` → **410 Gone**. Ship the SPA that stops those mutations **in the same release** as the 410.

## 8. On-demand install lifecycle (normative)

### 8.1 Distribution (ops, not Connect-time download of scripts)

Deploy **must** place onto every runner (same channel as today’s skill_bridge sync — rsync/image layer; **no script-bytes blob** in the RPC):

- `deploy/installers/*.sh` (bit-identical to the gateway’s reviewed tree)
- skill `tree` directories under `/var/lib/portunus/skills/…` when those artifacts are declared
- a **runner artifact pin map** file (versioned JSON) built at package/deploy time from TOML. Schema (normative):

```text
{ "version": 1,
  "entries": [
    { "recipe_slug", "artifact_id", "kind", "script_relpath?", "script_sha256?",
      "install_as", "content_sha256?" | "content_by_arch"?, "arch?" }
  ],
  "history": [ /* prior entry tuples retained across deploys until bake */ ]
}
```

Current + **history** entries are the allowlist. Deploy unions new pins; retain prior tuples for **at least 30 days or 5 deploys** (whichever longer) before prune — earlier prune risks `artifact_stale_snapshot`. Pin map is **CI-generated from TOML**, not hand-edited. Runner validates the pin map at startup (`pinmap_missing` / corrupt → runner refuse ready).

Runner **site registration** advertises `GOOS`/`GOARCH` (required; not only ensure-handshake). Gateway selects `content_by_arch[GOOS/GOARCH]` or, for flat pins, refuses ensure with `artifact_unsupported_arch` when runner arch ≠ recipe `arch`.

At ensure time the gateway sends **`ensure_artifact`** on the **existing authenticated runner control channel** (same trust as spawn — mTLS or runner token). Fields required by kind:

```text
# install_script
{ recipe_slug, artifact_id, kind: "install_script", script_relpath, script_sha256, content_sha256, install_as, goos, goarch }
# tree
{ recipe_slug, artifact_id, kind: "tree", install_as }
```

**Trust root:** deploy-published files + pin map on the runner. The runner **independently**:

1. Looks up `(recipe_slug, artifact_id)` in pin map **current∪history**; unknown → `artifact_pin_missing`.
2. Requires RPC `script_relpath`, `script_sha256`, `install_as`, and `content_sha256` to match **some** allowed tuple (normalized realpath; no `..`; prefix allowlist; no symlink escape). Mismatch → `artifact_path_rejected` or `artifact_stale_snapshot` if script unknown but artifact id known under a different hash set.
3. Opens the script with `O_NOFOLLOW`, hashes that **fd**, and executes the same fd/`fexecve` (or copies to same-fs scratch and execs the copy) — no path re-open TOCTOU.
4. After install, requires `sha256(install_as) == content_sha256` then writes the sidecar itself. Note: `content_sha256` bounds the **installed object**, not what the script may have written elsewhere under installer-writable dirs during the run.
5. Never fetches script bytes from the gateway.

Runner config `allow_install_egress` (bool, default true): when false, unsatisfied content hashes fail immediately without running the script (`artifact_install_failed`).

A compromised gateway can only request tuples present in the deploy pin map (including history). Signed pin maps / RPCs remain deferred — deploy channel integrity is an ops trust assumption called out here.

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
   - **`install_script`:** pin-map + path checks (§8.1); `flock` on `install_as.lock` under the installer-writable dir (wait ≤30s; no “stale lock”); **no-op** if file exists and `sha256(install_as) == content_sha256` and sidecar matches; if hash matches but sidecar **missing** → **write sidecar under the lock, no reinstall**; if hash disagrees with pin → unsatisfied (run script only if `allow_install_egress`, else `artifact_install_failed`); else run as installer uid with `PORTUNUS_ARTIFACT_ID`, `PORTUNUS_INSTALL_AS`, `PORTUNUS_RECIPE_SLUG`, plus minimal env (`PATH`, `HOME`, `TMPDIR`, `LANG`, optional CA/proxy); cwd = empty scratch **on the same filesystem as `install_as`**; **timeout 120s kills the process group**; script atomic-renames one file into `install_as`; **runner** verifies hash then writes sidecar under the lock; on failure return `{recipe_slug, artifact_id, exit_code, duration_ms, stderr_tail}` (`stderr_tail` last 4KiB or 100 lines, redact URL query tokens / `Authorization`); exit **78** → `artifact_unsupported_arch`. Empty snapshot while `recipe_slug` set → `artifact_snapshot_missing` (do not skip silently).
5. Waiters re-verify after lock; lock-wait timeout → `install_lock_timeout`. Other codes: `artifact_script_missing`, `artifact_hash_mismatch`, `artifact_pin_missing`, `artifact_path_rejected`, `artifact_stale_snapshot`, `artifact_install_failed`, `artifact_unsupported_arch`, `runner_unreachable`.

**Airgap:** content hash already satisfied → no network. Else script may need egress within 120s or fail.

**Provenance:** snapshot pins + deploy pin map + local script bytes. URL honesty lives inside reviewed scripts.

## 9. Migration

Split across releases:

**Release A — schema + TOML load (SQL remains the served catalog until B)**  
1. Add `catalog/apps/*.toml` for every current `DEFAULT_APPS` entry.  
2. Add nullable `servers.recipe_slug` + `artifact_snapshot` (JSON, nullable). New catalog Connects in A may write snapshot; SQL `GET /api/apps` still authoritative for the SPA until B.  
3. **Mandatory `migration_map.json`:** every existing `servers.id` → `"<recipe_slug>"` | `"custom"`. Generated draft may *suggest* matches from argv/url/oauth fingerprint vs TOML, but **nothing is applied from slug-string equality alone**. Operator signs off; **unmapped or conflicting rows abort**. Map schema: `{ "apps_digest": "<sha256 of sorted TOML recipe bytes>", "servers": { "<server_id>": "custom" | "<recipe_slug>" }, "catalog_apps_export": "<path>" }`. Apply aborts if live TOML digest ≠ `apps_digest`. Servers created after map generation require a refreshed map before cutover.  
4. Backfill `recipe_slug` + snapshot **only** from the signed map (catalog targets get current TOML artifact rows).  
5. **Mandatory** export of operator `catalog_apps` rows before any delete.  
6. Pre-flight optional: per runner site, check on-disk hashes vs pins for backfilled snapshots (warn on mass reinstall risk).

**Release B — cutover**  
7. Stop `ensure_defaults` / SQL catalog reads; SPA + API use TOML (`id` string slug); `/api/apps` mutations → 410.  
8. Delete `catalog_apps` rows only after export + bake; drop table later (deferred).

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
| Pin unknown | `artifact_pin_missing` |
| Path / tuple rejection | `artifact_path_rejected` |
| Snapshot pins not in pin-map history | `artifact_stale_snapshot` |
| Script missing / `script_sha256` mismatch | `artifact_script_missing` / `artifact_hash_mismatch` |
| Installed object hash ≠ pin | `artifact_install_failed` |
| Unsupported arch (gateway or exit 78) | `artifact_unsupported_arch` |
| Lock wait timeout | `install_lock_timeout` |
| Script timeout / nonzero | `artifact_install_failed` + stderr_tail |
| Runner control channel down | `runner_unreachable` |
| Custom skill_auth | Vault/session only |
| `/api/apps` mutations | 410 Gone |
| Custom create with reserved slug | 400 |
| Migration unmapped / digest mismatch | Migration job abort |
| `artifact_snapshot_missing` | Ensure/spawn error when recipe_slug set but snapshot empty |
| `pinmap_missing` / invalid | Runner not ready |

## 12. Testing

- Load fixture TOML dir; validate_spec + list; duplicate slug → startup error.
- Install script: fake arch + fake download; lock concurrency; hash short-circuit; hash mismatch refuses execute.
- Tree verify-only failure when `install_as` absent.
- Registry inject/skill_auth without `catalog_apps` rows; custom server skill_auth without recipe.
- Drift: change manifest **YAML body** → next inject sees it; change manifest **filename** in TOML → requires gateway restart; server argv unchanged.
- API 410 on app mutations; GET apps id === slug; reserved-slug rejection on custom create.
- `content_sha256` short-circuit; pin-map history accepts old snapshots; `install_as` uniqueness at startup.
- Migration map: bare slug equality never binds.

## 13. Deferred

- Pre-warm installers; async install + longer lock wait; Connect readiness UI.
- Host overlay recipes; control-plane script-byte sync; signed ensure RPCs.
- Full `[artifacts.pins]` URL table in TOML; multi-file / directory install kind.
- Drop `catalog_apps` table; detach-Orphaned-to-Custom; `recipe_rev`; SIGHUP catalog reload.
