# Portunus — TOML App Catalog (file recipes, DB for live data)

**Date:** 2026-11-13  
**Status:** design (audit-passed, round 28 — majority GO 4/6)  
**Decision:** Approach A — TOML recipes; DB for live MCP state; runner-executed on-demand installers.  
**Related:** `2026-11-09-portunus-design.md`.  
**Audit:** `docs/audit/` (janus `*-flash`; archive + SYNTHESIS).

## 1. Problem

SQLite-seeded Connect recipes mix code data with live state and fight operator edits.

## 2. Goals

1. Apps as `catalog/apps/*.toml`.
2. End state: DB holds **live** MCP state only. Release A keeps transitional `catalog_apps` for SPA int ids. Release B stops serving it (table retained only for SQL-read rollback). Physical `DROP TABLE` is a later release (Deferred).
3. Custom MCP = create-server only.
4. Native CLIs via runner install scripts (pinned), not multi-arch binaries in git.
5. Skill trees deploy-placed; ensure = directory exists and is non-empty.

## 3. Non-goals (v1)

- Merge overlays; `PORTUNUS_APPS_DIR` replaces packaged apps (reload = gateway restart).
- Blob sync; async install UI; signed pins; sandbox; attestation; airgap preseed tooling.
- `[artifacts.pins]` URL table; multi-file installs; slug-rename API; Omni changes.
- Non-reproducible package managers as `install_script` outputs (npm/pip/apt). v1 `install_script` = **byte-pinned static binary** only (`content_sha256` / `content_by_arch` must match finalized bytes).

## 4. Ownership

| Data | Store |
|---|---|
| Recipe + `[[artifacts]]` | TOML |
| Server + snapshots + health | `servers` |
| Secrets | vault |
| Transitional catalog rows (A only) | `catalog_apps` |

**Binding:** Catalog Connect sets immutable `recipe_slug` and `slug` (= recipe slug). **`servers.slug` is globally unique.** One live server per slug (hence at most one catalog server per recipe slug in v1). Custom: `recipe_slug` NULL; `slug` ∉ **all known** catalog stems (enabled or disabled-on-disk) → else **400** `reserved_slug`; if `connection = skill_bridge` → **400** `custom_skill_bridge` (customs may be oauth|http|webdav|stdio only).

**Connect idempotency:** Connect with an existing `slug` → **409** `server_exists` + that `server_id` (no second row), regardless of operator. DB **UNIQUE(`servers.slug`)** is authoritative (409 on violation).

**Snapshots (catalog):** JSON `artifact_snapshot[]` (`[]` OK). Catalog row with SQL NULL snapshot → ensure/spawn `artifact_snapshot_missing`. Custom rows keep `artifact_snapshot` NULL and **never** emit that code. `env_snapshot` from TOML `env_defaults` at Connect (`{}` OK). Freeze `command`/`args`/`url`/`oauth`, per-server vault binding ids, `manifest_name`, `manifest_sha256`. **Sites:** `preferred_site_id` = write-once Connect-time site (null if auto-picked); `artifact_site_id` = **mutable** current ensure site (set at Connect; updated when `ensure?site=` succeeds).  
**Custom:** optional `env_snapshot` on create — TOML-string values; keys ∉ that server’s secret names / `PORTUNUS_*`; ≤32 KiB (catalog `env_defaults` same).

**Vault binding:** operators pre-set **global** vault keys `app:{slug}:{NAME}` (catalog) or `custom:{slug}:{NAME}` (custom create). Pre-persist `secret_missing` checks those globals. At persist (Connect **or** custom create), gateway **copies** each value into `server:{id}:{NAME}` and freezes those binding ids. DELETE purges only `server:{id}:*` (globals untouched). Vault-write failure mid-persist → roll back server row + `persist_failed`. Post-Connect edits to globals do not mutate existing bindings (rotate = write `server:{id}:{NAME}` via vault ACL; Connect to existing slug is 409 — delete+recreate if rebound needed).

**Runtime authority:** frozen fields + per-server vault + `env_snapshot`. **Manifest:** gateway sends manifest bytes + sha on spawn/inject RPC; runner verifies sha and exposes bytes via **memfd** (or equivalent fd) to the child — not a durable runner path. Sha mismatch → spawn-RPC `manifest_drift`; schema fail → `manifest_invalid`. Ops: `portunus catalog rehash-manifests` / `rehash-artifacts`. Orphan inject → spawn-RPC `recipe_missing`.

**Element shapes:**  
`install_script` `{id,kind,script,script_sha256,install_as}` plus XOR: either (`arch` + `content_sha256`) or non-empty `content_by_arch` (both fields of the unused arm absent).  
`tree` `{id,kind,path,install_as}` — `path` packaging hint only; runtime uses map `install_as`.

## 5. Layout

`catalog/apps/*.toml`, `deploy/installers/*.sh` (mode **0555**), pin-map JSON (**0444**), `third_party/…` → `/var/lib/portunus/skills/…`, packaged manifests.

## 6. TOML + validation

```toml
slug = "quark"
label = "夸克网盘"
description = "…"
connection = "skill_bridge"  # oauth|http|webdav|stdio|skill_bridge
popular = false
categories = ["Storage"]
tags = ["quark", "drive"]
sort_order = 76

[spec]
manifest = "quark.example.yaml"
secrets = ["QUARK_COOKIE"]           # vaulted env names
# env_defaults: TOML strings only; keys ∉ secrets and must not start with PORTUNUS_; object ≤32 KiB
# other connection fields: same meanings as gateway validate_spec(connection, spec) today
# (normative copy lives in gateway; TOML must pass that function)
# slug = filename stem; slug ∈ [a-z0-9-]{1,64} (lowercase); verify rejects otherwise

[[artifacts]]
id = "rclone"
kind = "install_script"
script = "install-rclone.sh"
script_sha256 = "<hex>"
install_as = "/var/lib/portunus/bin/rclone"
arch = "linux/amd64"          # v1: linux/* only (== runner_sites.goos+"/"+goarch)
content_sha256 = "<hex>"
needs_egress = true             # optional; default true — site pick uses this
# XOR mandatory for install_script: exactly (arch+content_sha256) OR non-empty content_by_arch
# (no archless install_script). content_by_arch keys must be linux/<GOARCH> (verify rejects other GOOS).
# Identical bytes on two arches → duplicate the hex under each key. artifact_id unique per TOML.
```

**Filename:** stem **must equal** TOML `slug` else verify fail / load disable. TOML file ≤512 KiB.

**Canonical `install_as`:** absolute path; UTF-8; collapse `.`/`..` lexically; reject empty, relative, NUL, or symlink components in the string (no `realpath` — lexical only). Same function used for verify share-check, lock hash, helper destination, and pin-map rows.

**`portunus catalog verify` (CI + gateway load):** XOR; ≤32 artifacts/recipe. **Shared `install_as` (canonical):** distinct `(recipe_slug, artifact_id)` install_script pairs may share **iff** identical `script_sha256` + content hex on every overlapping arch; else later-sorted recipe disabled. Intra-recipe clash → verify fail. **Cross-kind** (tree vs install_script) identical canonical `install_as` → verify fail (no share). Trees: full-path uniqueness among trees. Unique `artifact_id` per TOML. Prefix by kind (`/var/lib/portunus/bin/` vs `/var/lib/portunus/skills/`); parent dirs must be root-owned. Scalar = one `arch`; multi-arch ⇒ `content_by_arch`. Manifest must exist. **Parity:** packaged/release-tree TOML ↔ pin-map; `PORTUNUS_APPS_DIR` overlays are **exempt** from release pin-map parity (operator-owned; `doctor` warns). Runner digest mismatch → `pinmap_stale`. New TOML vs existing custom → recipe disabled until custom DELETE’d. **Repair:** `rehash-*`, or delete+Connect.

**Load:** TOML directory is read at **gateway start** only (`PORTUNUS_APPS_DIR` / packaged; change ⇒ restart — §3). On-disk checks (`manifest_missing`, stem≠slug, charset, shared-`install_as`, empty arch intersection) run **only at load/verify**. **DB-dependent re-eval** (on custom create/delete/Connect): duplicate-slug vs live customs, custom-stem collision disable/re-enable. New Connect to disabled slug → 400 `recipe_disabled`; Ensure when recipe disabled or TOML absent from loaded set → 422 `recipe_disabled`. Custom DELETE → re-enable same-stem TOML if clear. `env_defaults` = TOML strings; keys ∉ `PORTUNUS_*` / `secrets[]`. **`apps_digest` v1:** sha256 of concatenation of frames `stem || 0x00 || len_be_u64 || bytes` for every `*.toml`, sorted by stem ascending (bytewise); includes unparsable.

## 7. APIs

- `GET /api/apps` — enabled recipes with `slug` always; `"catalog_mode": "A"|"B"`; per recipe: arch set, `needs_egress`, `site_pick_ok` (true iff ≥1 registered site passes the §7 auto-pick predicate including egress). **A:** `id` = SQL int for recipes that already have a live catalog server or an upserted identity row; upsert `catalog_apps` on successful catalog Connect (and on load for recipes that already have a live server) so A→rollback stays current; pre-Connect TOML-only may be `null` — SPA Connects by `slug`. **B:** `id` = `slug`.
- Connect `POST`: **A** accepts `slug` always; legacy int `id` only when `catalog_apps` row exists. Both `id`+`slug` → **400** `bad_input`. Int with no row → **400** `bad_input`. **B:** slug required. Optional `site_id`. After site pick: set `artifact_site_id`; set write-once `preferred_site_id` if caller passed `site_id`, else null. Auto-pick sorts `preferred_site_id` first when still eligible, else by `site_id`. `ensure?site=` on success updates `artifact_site_id` only. Ensure without `?site=` uses `artifact_site_id` with arch-OK re-check; deregistered / not arch-OK → auto-pick (pre-persist none → 503; after persist → Connect 201 degraded / Ensure 503). `site_unknown` 422 only for caller `site_id` missing.
- Connect order: `bad_input` → `server_exists` 409 → `recipe_unknown` 404 → reserved/custom/`recipe_disabled` 400 → `secret_missing` 422 → site validation → persist (copy vault; on failure purge any partial `server:{id}:*` then `persist_failed`) → ensure. Single-flight: Connect waits remaining wall; still held → 201 degraded `ensure_in_progress`. Concurrent Ensure → 409 `ensure_in_progress`.
- `POST /api/servers` (custom): `secrets?` = name list → globals `custom:{slug}:{NAME}`; `env_snapshot?` as §4; optional `site_id` (stdio only). Order: `bad_input` → `server_exists` 409 → `reserved_slug` → `custom_skill_bridge` → `secret_missing` 422 → **if connection=stdio:** site pick (linux; none → 503 `no_runner_site`) and freeze `artifact_site_id`; **oauth|http|webdav:** no site required (`artifact_site_id` null) → persist+vault copy (re-check globals inside txn; miss → 422 `secret_missing`, no row) → **201** `{server_id, health:"healthy"}`. Ensure = **200** no-op always.
- `POST /api/servers/{id}/ensure` — int or `?slug=` (mismatch → 400 `bad_input`). Single-flight → else 409. Custom → 200 no-op. Ensure order: `server_unknown` → `ensure_in_progress` → `recipe_disabled` → site/arch → pinmap/runner → artifact. Max **32** artifacts/recipe. Ensure errors include `server_id` when known.
- `DELETE /api/servers/{id}` — purge `server:{id}:*` + snapshots; frees slug; re-eval; A may keep `catalog_apps` identity (stale id until reused — accepted). No `install_as` reclaim (Deferred).
- `GET /api/admin/snapshot_refs` — `{gen, apps_digest, refs:[…]}`; `gen` = persisted monotonic counter (survives restart).
- **Who execs MCP children:** the **runner**. Spawn RPC: `{server_id, connection, command?, args?, url?, oauth?, env_snapshot, vault:[{name,value}], manifest_bytes?, manifest_sha256?, artifact_site_id?}` (memory-only; never logged; not on runner disk). Catalog skill_bridge includes manifest memfd; customs stdio use frozen `command`/`args`; remote customs need no site. Precedence vault > `env_snapshot`. Install scripts never see vault. Runner host compromise is in the TCB (Deferred: attestation).
- **Runner process:** supervisor root drops to `portunus-install` for script exec; MCP children as `portunus-mcp` (not in group `portunus-install`). Installer cannot read runner mTLS/token material (root-only paths).
- Legacy catalog mutation routes (if any remain mounted) → **410** `gone` from Release **A** onward.

**Sites** (`runner_sites`): `site_id`, `goos`, `goarch`, `allow_install_egress`, `runner_instance_id`. v1 registration rejects `goos≠linux`. Pin-map digest compared per RPC (runner accepts N or N-1). Registration auth = existing Portunus runner mTLS/token control-plane.

**Per-artifact arches:** for each `install_script`, the set `{arch}` or `keys(content_by_arch)` (linux/* only). Trees: no arch constraint.

**Site pick (deterministic):** a site is arch-OK iff **for every** install_script artifact, `site.goos/site.goarch` ∈ that artifact’s arch set (tree-only → any registered linux site).  
**Recipe `needs_egress`:** ∨ over install_script artifacts (trees ignored). **Auto site pick** (Connect / ensure without `?site=`): if recipe `needs_egress`, require `allow_install_egress=true`. Tree-only: any linux site. Prefer `preferred_site_id` when eligible, else sort by `site_id`; none → **503** `no_runner_site` (pre-persist). **Empty arch intersection** across a recipe’s install_script artifacts → **verify fail / load disable** (never reaches Connect as `no_runner_site`).  
**Explicit site:** unknown → **422** `site_unknown`; not arch-OK → **422** `artifact_arch_unavailable`. Egress-false + needs_egress → admit at Connect; short-circuit miss at ensure → Connect **201** degraded / Ensure **422** `artifact_egress_disabled`.  
**Sticky site:** ensure without `?site=` keeps `artifact_site_id` if still arch-OK; if still arch-OK but egress-ineligible for a needed install → **422**/201 `artifact_egress_disabled` (no silent auto-pick). Auto-pick only when site deregistered or not arch-OK.  
**§10 note:** pre-persist site/arch failures never return `server_id`. Post-persist ensure-phase `no_runner_site` / arch miss → Connect 201 degraded or Ensure 503/422 with `server_id`.

## 8. Install lifecycle

### 8.1 Pin map

**Distribution:** same deploy channel as installers (image layer / rsync to every runner). CI builds `{version, digest, entries[], history[]}` into the release artifact; runners never pull pins from the gateway. Bootstrap = first deploy; updates = next deploy (digest change → `pinmap_stale` until rolled out).

`install_script` row: `recipe_slug,artifact_id,kind,script_relpath,script_sha256,install_as,arch,content_sha256,needs_egress`.  
`tree` row: `recipe_slug,artifact_id,kind=tree,install_as` under skills (**no arch**; **no finalize** — deploy places tree; ensure only checks non-empty dir). Lookup `(recipe_slug, artifact_id, kind=tree)`.  
Expand `content_by_arch` to scalar rows keyed by `(recipe_slug, artifact_id, arch)`. Pin rows derive from **release-tree TOML**; `snapshot_refs.refs` only gate history pruning. History ≥30d and ≥5 deploys. **Bake (post-deploy only):** after gateway loads the new release, CI calls `GET /api/admin/snapshot_refs` and rejects if `apps_digest` ≠ that release’s TOML-tree digest. Refuse to drop history still in `refs`. **Rollout:** each runner keeps pin-map files for digests **N and N-1**; RPC digest matches either → OK; else `pinmap_stale`. Deploy runners (new map as N, previous retained as N-1) before gateway flips.

**Runner prerequisites (mandatory):** `/proc/self/fd`, `O_NOFOLLOW`, flock, uid `portunus-install` writable **only** on staging + lock dir; helper binary `/usr/libexec/portunus/install-finalize` owned root:root `0550` (root supervisor only — not setuid for scripts). Helper socket listener started by runner supervisor at boot (absent → `artifact_finalize_failed`). Pin-map files replaced atomically (`rename`+`fsync`); readers see N or N-1 whole files only.

**Lock + finalize:** lock path everywhere = `locks/portunus-install/<sha256(canonical install_as)>.lock` (same canonicalization at flock, helper, intent). Supervisor (root) `flock(LOCK_EX)` **before** dropping to `portunus-install` for script exec; after script, **root supervisor** calls finalize (scripts never connect). **Helper** socket `/run/portunus/install-finalize.sock` mode **`0600` root-only** + SCM_RIGHTS; `SO_PEERCRED` peer uid = **0**. Caller passes staging fd + lock fd; helper requires lock path == the formula above. Also: `artifact_id`, `recipe_slug`, `arch` (install_script only), expected hex. Lookup install_script by `(recipe_slug, artifact_id, arch)`; tree by `(recipe_slug, artifact_id, kind=tree)`. Destination = canonical map `install_as` under bin via `openat`/`renameat` on bin dirfd (`O_NOFOLLOW`; reject escape). **Copy order:** `openat` create `.tmp.<pid>` exclusive → copy from staging fd while hashing → if hash≠expected unlink tmp and fail → `fsync` → `fchmod`/`fchown` root:root `0755` → `renameat` over basename → sidecar `basename.sha256` (64 lowercase hex + `\n`, `0444`) via temp+`renameat` (never hash then re-open staging). Else `.rejected.<pid>`. Success → remove staging leaf. GC >24h. Trees never enter this helper. → `artifact_finalize_failed` (502). Script timeout: process-group SIGTERM/KILL. Pin-map N/N-1 by digest filename.

**Also:** bin `root:root` `0755`; locks `1770` root:`portunus-install`; staging `0750`; skills `root:root` `0755`; pin map `0444`. One runner agent/host (mTLS). Ensure-time hash/egress → §10 HTTP; spawn re-check → RPC only. **Inject** = memfd + per-server vault. **Health:** catalog persist+artifacts → `pending_ensure` (customs create as `healthy`); success → `healthy`; fail/cancel/abandon → `degraded` (`connect_aborted` **stored only**, not a Connect response cell); startup re-queues once. Disconnect → SPA polls `GET`.

### 8.2 Runner

Each ensure RPC includes gateway’s pinmap digest (from the same release bundle the gateway loaded). Runner: pin-map file absent → `pinmap_missing`; unparsable/schema fail → `pinmap_invalid`; digest ≠ RPC → `pinmap_stale` (**503**, no `Retry-After`, operator redeploy required — not auto-retried). Auth = same mTLS/runner token as spawn.

Intent: `{recipe_slug, artifact_id, kind, goos, goarch, pinmap_digest, script_sha256?, content_sha256?, install_as?}` — arch key = `goos+"/"+goarch` for install_script; trees omit arch. Runner **authorizes** from local map: install_script `(recipe_slug, artifact_id, arch)`; tree `(recipe_slug, artifact_id, kind=tree)`. Intent hashes/path ≠ map → `artifact_stale_snapshot`. Paths from map only.

| Trigger | Code |
|---|---|
| No map row for install_script key / tree key | `artifact_pin_missing` |
| install_script arch missing from expanded rows | `artifact_arch_unavailable` |
| goos/goarch ≠ runner | `artifact_identity_mismatch` |
| map.install_as fails prefix (bin vs skills by kind) | `artifact_path_rejected` |
| script file absent | `artifact_script_missing` |
| fd hash ≠ map.script_sha256 | `artifact_hash_mismatch` |
| exit 78 | `artifact_arch_unavailable` (precedes generic failed) |
| other nonzero / timeout | `artifact_install_failed` / `artifact_install_timeout` |
| finalize hash fail | `artifact_hash_mismatch` (helper rejects staging → `.rejected.<pid>`) |
| egress false and short-circuit miss | `artifact_egress_disabled` |
| flock wait exceeded | `install_lock_timeout` |

**Short-circuit predicate:** `install_as` is a regular file ∧ `sha256(install_as)==map.content_sha256` ∧ sidecar `install_as.sha256` exists and matches that hex.

**install_script:** as `portunus-install`, open script `O_NOFOLLOW` → verify fd hash → `fexecve`/`/proc/self/fd/N` (shebang: keep fd open and exec `/proc/self/fd/N`). Script writes payload only into staging dir (`PORTUNUS_STAGING`). Root supervisor then calls `install-finalize` (all bin mutations; scripts never connect). Env: `PORTUNUS_ARTIFACT_ID`, `PORTUNUS_INSTALL_AS`, `PORTUNUS_RECIPE_SLUG`, `PORTUNUS_STAGING`, `PORTUNUS_GOOS`, `PORTUNUS_GOARCH`, PATH/HOME/TMPDIR/LANG/CA/proxy — **no vault**. Lock file = `locks/portunus-install/<sha256(canonical install_as)>.lock`; wait≤120s then run≤120s. Egress false: no exec unless short-circuit. Audit best-effort to gateway (failure logged, does not fail ensure). `content_sha256` = trust anchor for upstream bytes.

**tree:** map `install_as` → `-d` && non-empty else `artifact_tree_missing`.

`allow_install_egress` default **true**. Airgap = site flag false + operator pre-stage + explicit `?site=` (see §7).

### 8.3 Budgets / Connect

**Fan-out:** one ensure RPC per snapshot artifact; distinct `install_as` concurrent with **cap 8**. Wall = **⌈n_distinct/8⌉ × 240s** (each wave ≤120 wait + 120 run). Client timeout ≥ wall + 60s. Shared `install_as`: flock; wait >120s → `install_lock_timeout`. Client disconnect cancels outstanding RPCs; in-flight may finish — success → `healthy`, else stays `degraded` until a later successful ensure (`GET /api/servers/{id}` exposes health). Partial failure returns first failing code; prior successes stay (idempotent retry). Vault→MCP transport: runner sets child env from spawn RPC memory then zeros its copy; same-uid `portunus-mcp` siblings can read `/proc/<pid>/environ` — **accepted v1 residual** (Deferred: per-server uid).

**Connect (waits for ensure before response):**

1. Validate body/secrets/globals (§7 order) then site pick.
2. Persist row + copy vault bindings (transactional).
3. Ensure ≤ budgets (same single-flight as ensure endpoint).
4. If persisted: always have `server_id` (client may miss the HTTP body on disconnect — poll `GET`).  
   - ensure OK → 201 `{server_id, health:"healthy"}`  
   - ensure fail → 201 `{server_id, health:"degraded", code, message}` — client `POST .../ensure`  
   - fail before persist → 4xx/5xx without `server_id`  
   - `connect_aborted` is a **stored** health code after cancel/abandon (not a response the cancelled client necessarily receives)

**Ensure endpoint statuses:** same `code` as above with HTTP from §10 (not wrapped in 201).

## 9. Migration

**`apps_digest`:** identical algorithm to §6 (sha256 over length-prefixed `stem\0len\0bytes` frames, sorted by stem, every on-disk `*.toml` including unparsable; ignore DB disable flags). Gateway recomputes after load; bake = post-deploy compare only (§8.1).

**Ordering:** ship/load the TOML tree (matching `apps_digest`) **before** migration apply. Migration takes an exclusive DB lock (reject Connect/Delete/ensure with 503 `persist_failed` / `ensure_in_progress` until done). Dry-run: fingerprint = `(connection, command, args, url, oauth_public)` vs TOML; match → that recipe; former catalog row whose TOML was **removed** → quarantine reason `recipe_absent` (not custom). True orphans → candidate `custom`. Two servers → same recipe → oldest wins; losers moved to quarantine with **`slug` cleared** (`slug` NULL / `quarantined:<id>` marker outside UNIQUE) so UNIQUE can apply (`duplicate_slug`). Stem-colliding custom vs TOML: keep custom, disable TOML until custom deleted. Operator map overrides. Pre-apply: `*.pre-migration.bak`. **Digest mismatch always aborts**. Without `--partial`, any unmapped / argv conflict / `skill_bridge_custom` / `duplicate_slug` / `recipe_absent` → abort. **`--partial`:** apply clean; quarantine JSON+DB `{ "version": 1, "ids": [...], "reason": { "<id>": "unmapped"|"argv_conflict"|"skill_bridge_custom"|"duplicate_slug"|"recipe_absent" } }`. Existing catalog skill_bridge → TOML; removed TOML on already-migrated server → spawn `recipe_missing`. Backfill argv/url/oauth from server else TOML; artifacts/env/manifest sha from TOML. Seed pin history. Export `catalog_apps`. Schema: new table + copy for UNIQUE if needed. `catalog_mode` = `A`|`B`.

**A SPA:** Connect by `slug`; may translate int ids via `GET /api/apps`.  
**B:** apps `id`=`slug`; servers keep int PK; `POST /api/servers/{id}/ensure` accepts int PK **or** `slug=` query for the server’s slug. 410 app mutations; SQL catalog reads off (`catalog_apps` rollback-only). Rollback: SQL-read flag + old SPA.

## 10. HTTP matrix

Envelope: `{ "code": "<exact name>", "message": "...", "server_id"?: int }`.

| Code | Connect | Ensure | Retry |
|---|---|---|---|
| (success) | 201 healthy | 200 | — |
| `no_runner_site` | 503 pre-persist; 201 degraded if ensure-phase re-pick none after persist | 503 | yes |
| `persist_failed` | 503 (no server_id) | — | no |
| `pinmap_stale` | 201 degraded | 503 (no Retry-After; redeploy) | no† |
| `pinmap_missing`, `pinmap_invalid`, `runner_unreachable` | 201 degraded | 503 | yes |
| `install_lock_timeout` | 201 degraded | 409 | yes |
| `server_exists` | 409 + server_id | — | no |
| `recipe_unknown` | 404 | — | no |
| `reserved_slug`, `custom_skill_bridge`, `bad_input` | 400 (pre-persist; also custom create) | — | no |
| `recipe_disabled` | 400 pre-persist; 201 degraded if disabled after persist | 422 (disabled or TOML absent) | no |
| `secret_missing` | 422 pre-persist (global absent at check or copy) | — (spawn RPC if binding later deleted) | no |
| `artifact_arch_unavailable` | 422 pre-persist; 201 degraded if ensure-phase after persist | 422 | yes‡ |
| `artifact_egress_disabled`, `artifact_stale_snapshot`, `artifact_snapshot_missing`, `artifact_tree_missing`, `artifact_path_rejected`, `artifact_identity_mismatch`, `artifact_hash_mismatch` | 201 degraded (Connect ensure-time only) | 422 | no* |
| `recipe_missing`, `manifest_invalid`, `manifest_drift` | — (spawn/inject RPC only) | — | no* |
| `connect_aborted` | — (stored health only) | — | no |
| `ensure_in_progress` | 201 degraded (Connect waited out single-flight) | 409 Retry-After:5 | yes |
| `artifact_install_failed`, `artifact_install_timeout`, `artifact_script_missing`, `artifact_pin_missing`, `artifact_finalize_failed` | 201 degraded | 502 | yes |
| `server_unknown` | — | 404 | no |
| `site_unknown` | 422 pre-persist (caller `site_id` only) | 422 (caller `site_id` only) | no |
| `gone` | 410 on `/api/apps` mutations only | — | no |

`manifest_missing`: load-time disable only. `recipe_disabled`: Connect **400** if slug disabled; Ensure **422** if server’s recipe later disabled.  
\*ops (`rehash-*` / reconnect). †redeploy only. ‡other site — only when request used auto-pick or can pass a different `site_id`; caller-fixed ineligible `site_id` → Retry=no. `Retry-After` only on Ensure 4xx/5xx when Retry=yes: `install_lock_timeout`/`ensure_in_progress`/`artifact_arch_unavailable` 5s; 502 10s; `runner_unreachable`/`pinmap_missing`/`pinmap_invalid`/`no_runner_site` 30s. Never on Connect 201 / `persist_failed` / `connect_aborted` / `pinmap_stale`. **Parity** disable at load; runner `pinmap_stale` separate. Empty arch intersection → verify fail. Envelope `{code,message,server_id?}` on errors. `catalog_mode` on `GET /api/apps` with label/description/categories/tags/popular/sort_order. `needs_egress` = recipe OR.  
**Health on `GET /api/servers/{id}`:** `{health, code?, artifact_site_id?}`. Zero-artifact/custom → `healthy`. SPA after drop: poll `GET` then `ensure`.  
**Airgap:** auto-pick never selects egress-false for `needs_egress`; operator pre-stages root-owned binary at map `install_as` + sidecar `install_as.sha256` (64 lowercase hex + `\n`, `0444`) on that site; Connect/ensure with explicit `site_id`. v1: `linux/*` only.  
**410 `gone`:** legacy catalog mutation routes from Release A. Tree `path` never in RPC/pin map. Trees deploy-materialized; ensure checks non-empty dir.  
**Producers:** `persist_failed` = DB/vault-copy failure (purge partial bindings; no row); `runner_unreachable` = ensure RPC transport/timeout; `bad_input` = schema/body; `manifest_*` = spawn RPC. Snapshot_refs = existing admin auth. Vault rotate = write `server:{id}:{NAME}` (same vault ACL as today); Connect cannot rotate (409).  
**Accepted v1 residuals** (do **not** block GO): async/202 Connect, sandbox beyond staging+helper, signed pins, per-server MCP uid, `install_as` reclaim, tree content hashes, airgap preseed tooling, attestation — §3/§12.

## 11. Testing

Disable/collision re-eval; shared `install_as` clash; explicit-site airgap ensure; pinmap_stale; path_rejected; exit-78; degraded 201; ensure HTTP; migration abort; A slug Connect; 409 idempotent; content hash; fexecve+finalize helper; skills non-writable; spawn re-check.

## 12. Deferred

Async UI / 202-pending Connect for large fan-out; SIGHUP; overlays; signed pins; sandbox; attestation; tree content hashes; multi-file installs; slug rename; `recipe_rev`; drop `catalog_apps`; airgap preseed tooling; replace runner auth system; per-server MCP uid (environ isolation); reclaim `install_as` binaries on last server delete.
