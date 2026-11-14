# Portunus TOML Catalog Spec — Audit

## 1. Verdict
**GO WITH FIXES** — sound direction, but recipe-drift semantics, the installer trust boundary, and the §8.2 tree contradiction must be resolved before implementation.

## 2. Critical risks
- **Catalog↔server split is unresolved on drift.** §4 puts spec in `servers` after Connect; §7.3 resolves manifest by slug via `AppCatalog` at runtime. If TOML changes command/env/manifest, existing servers silently change behavior — or break. Pick one source of truth.
- **Installer security is under-specified.** §8 says "allowlisted path" and "path traversal rejected" but never says *which layer* enforces it, what user the script runs as, or how the runner verifies script integrity in a multi-runner fleet. Success = "install_as exists and **optional** sidecar matches" — an attacker-controlled or buggy script that just touches the file passes.
- **Migration deletes operator data silently.** §9.3 wipes all `catalog_apps` rows; today operators create custom recipes via CRUD. No export path. Also unstated: does `servers` hold a FK/UUID to `catalog_apps`? Dropping rows breaks existing joins unless a `servers.slug` column migration is specified.
- **skill_auth for custom skill_bridge servers.** Custom servers created without a recipe have no slug; §11 routes unknown slug → 422, so they can never re-auth. Contract gap.
- **Connect UX on installer failure** leaks truncated script stderr — pinned-URL fetch errors can echo tokens. Needs redaction, not just truncation.

## 3. Contradictions / stale claims
- **§8.2 contradicts itself:** "sync tree (rsync-like copy from image/bundle)" vs "v1 requires the tree present on the runner image… Connect does not stream trees." Is `tree` a runtime artifact step or a deploy-time no-op check? Non-normative as written.
- **§8 trigger** ("before first child start… or at Connect when the path can wait") is vague; two code paths, undefined which is normative.
- **§7** "404 or gone" — undecided contract.
- **§6** artifact `script = "deploy/installers/…"` — resolution base (repo root? `PORTUNUS_APPS_DIR` parent?) unspecified.

## 4. Missing requirements
- Runner trust boundary: script distribution, execution user, fs scope, env allowlist, stdout/stderr sanitization.
- Recipe versioning/pinning; no `recipe_version`.
- Lock file location, stale-lock recovery, lock scope per-`install_as` vs per-runner.
- Airgap: install scripts fetch pinned URLs; airgapped Connect fails with no offline story beyond deferred §13.
- SPA migration from stored UUID ids to slug ids (bookmarks/localStorage).
- Audit log for install runs.
- Windows unsupported (`flock`) — unstated platform assumption.

## 5. Architecture notes
- **Gateway builds + hashes, runner executes.** Gateway reads TOML, resolves script path, ships bytes+SHA256 over existing control plane; runner verifies hash, runs under dedicated low-priv user with `flock` on `install_as.lock`, cwd=scratch, 120s timeout, redacted stderr tail back. This avoids runners trusting gateway filesystem state and keeps the allowlist enforcement at the gateway (path resolution) *and* runner (hash check).
- **Trees:** normative choice — deploy guarantees presence; runtime step = verify `install_as` exists + hash matches, else fail with deploy-pointer error. Drop the rsync wording.
- **Pre-warm hook** (§13) should be a deploy step, not optional bolt-on.

## 6. Top 5 concrete edits
1. Add §"Recipe pinning": Connect copies full spec + `recipe_version` into `server`; inject/skill_auth read the server row; `AppCatalog` serves Connect cards and new servers only.
2. Rewrite §8 security as normative: gateway-side path allowlist + script hash; runner-side hash verify, dedicated user, env allowlist, stderr redaction; **mandatory** `.sha256` sidecar.
3. Resolve §8.2: trees = deploy-guaranteed, runtime verify-only; delete "rsync-like copy" language; state behavior when hash is stale (fail, not re-sync).
4. §9 migration: add `servers.slug` column migration, remove any `catalog_apps` FK, specify export-or-discard policy for existing operator recipes, and gate table drop on zero servers referencing old ids.
5. §11: add rows for custom-server skill_auth (define behavior), stale tree hash, stale lock, and airgapped installer failure with pointer to pre-warm; specify artifact/script path resolution base.
