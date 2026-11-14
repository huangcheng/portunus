# Audit: TOML App Catalog spec

**1. Verdict — GO WITH FIXES:** the recipe/live split and runner-executed installer model are sound, but the no-op short-circuit is unimplementable as specified and two states (Orphaned recipe, artifact satisfaction) are undefined.

**2. Critical risks**
- **No-op hash check has no source.** The RPC carries `script_sha256` (hash of the *script*). §8.3's "install_as + sidecar match expected content hash" never defines where the expected installed-object hash comes from. Add `install_sha256` to `[[artifacts]]` and the RPC.
- **"Orphaned recipe" is undetectable.** Once a slug is removed from TOML, nothing records it was ever shipped; §10's label needs tombstones. Worse, §8.3 step 1 treats slug-not-in-catalog as custom and *skips* artifacts, contradicting §4's `recipe_missing`.
- **Artifact satisfaction scope undefined.** Per-server or per-runner-site? A fleet re-spawning a server on a different site must re-ensure; per-server state yields a bare missing-binary failure.
- **`install_as` drift.** Frozen argv snapshots reference `install_as` paths; renaming in TOML breaks existing servers. §4's drift policy omits it.
- **Migration id swap.** SPA moves from integer `catalog_apps.id` to slug strings; a mixed-version SPA during rolling deploy breaks Connect cards. No compat note.

**3. Contradictions / stale claims**
- §8.3 step 1 (skip artifacts, custom) vs §4/§10 (`recipe_missing`, Orphaned label).
- §4 says non-secret `env_defaults` are live from TOML, but nothing distinguishes secret from non-secret entries or forbids literal secrets in `env_defaults`. `validate_spec` must enforce this.
- §8.1 claims deploy is "bit-identical to the gateway's reviewed tree," but verification exists only for scripts. `tree_digest` — computed where? Gateway-side hashing of its own checkout won't match runner deployments.

**4. Missing requirements**
- Startup validation that `manifest` exists in `packages/portunus_skill_bridge/manifests/`; slug case-collisions on case-insensitive dev filesystems.
- Connect-time ensure budget: 120s × N artifacts synchronous; no overall deadline or partial-failure semantics.
- Installs made by older scripts without sidecars fail the no-op check and hit the network — an airgap regression path; specify handling.
- flock is per-host only; note it doesn't guard shared-storage runners.

**5. Architecture notes**
- Runner-executed install is the right call: gateway ships no bytes, runner trusts disk + reviewed hash — provenance lives in the reviewed tree. Residual: pin rot (moved URL) surfaces as timeout, not a distinct code; consider `pin_unreachable`.
- Locking (flock, waiters re-verify) is correct; add a lock-file ownership check to prevent stale-lock squatting.
- Keep `tree` verify-only; either drop `tree_digest` or compute it at deploy-publish time.

**6. Top 5 edits**
1. Add `install_sha256` to `[[artifacts]]` + RPC; define the no-op short-circuit against it.
2. Add shipped-slug tombstones (or drop "Orphaned recipe"); reconcile §8.3 step 1 with `recipe_missing`.
3. Specify per-runner-site satisfaction and re-ensure on runner change.
4. Startup-validate manifest existence; classify `install_as` as frozen-at-Connect; forbid literal secrets in `env_defaults`.
5. Define the ensure budget (overall deadline, per-artifact cap, partial failure) plus the SPA slug-id migration note.
