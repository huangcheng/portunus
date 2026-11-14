# Audit — Portunus TOML App Catalog

**1. Verdict:** GO WITH FIXES — the snapshot + deploy pin-map trust model is sound, but the lock-wait/install-time mismatch, `env_defaults` freeze ambiguity, and unsigned migration inputs must be fixed first.

**2. Critical risks**
- **Catalog↔server split:** `recipe_slug` + `artifact_snapshot` with no FK is right, but "snapshot empty and recipe present" has no contract — artifacts silently skipped → later `spawn_failed` with no clear cause.
- **Installer security:** strongest part (runner-side `script_sha256` check, pin-map current∪history, installer uid, no script bytes over RPC). Residual: runner-advertised GOOS/GOARCH is unauthenticated; a lying runner picks wrong pins.
- **Multi-arch:** flat-pin vs `content_by_arch` XOR at startup is fine; exit-78 mapping covered.
- **Migration:** signed map pins *slugs*, not the TOML revision — snapshot generated at apply time can differ from what the operator approved.
- **Skill-bridge inject:** manifest body re-read at inject vs §7 "startup-immutable" record — `env_defaults` is ambiguous (§4 says "at inject", §7 implies cached at startup).
- **Connect UX:** flock wait ≤30s vs install timeout 120s — first-spawn installs routinely kill waiters, including Connect.

**3. Contradictions / stale claims**
- Sidecar missing but `sha256(install_as)` already verified → "fail closed, re-run if egress allowed". The hash is known; the sidecar is derivable — no reinstall needed.
- §11 omits `pinmap_missing` / runner refuse-ready (defined only in §8.1).
- Release A serves SQL while writing TOML snapshots — two authorities can diverge within A.
- §2 "not hashed in-gateway" vs §8 hashing: only true for trees; wording overstates.

**4. Missing requirements**
- Startup check: TOML slug colliding with an existing custom `servers.slug` — declared "ops config" but never enforced.
- Egress-allowed configuration for the fail-closed reinstall branch.
- Pin-map history pruning rule (prune too early → `artifact_stale_snapshot` for un-reconnected servers).
- 410 rollback story if the SPA ships late; `migration_map` digest pinning.

**5. Architecture notes**
- Install on the **runner** (not gateway) is correct: matches the deploy trust root, keeps egress local, and lets the runner independently verify. Locking is sound but mis-sized. Airgap: satisfied hash → offline; otherwise script needs egress within 120s — acceptable, must be stated in ops docs.

**6. Top 5 edits**
1. Set lock wait ≥ install timeout (or queue spawn behind in-flight ensure); align Connect's wait.
2. Freeze `env_defaults` in the snapshot (or explicitly declare live + test it); resolve §4/§7 wording.
3. Add `apps_digest` to `migration_map.json`; abort if TOML digest mismatches at apply.
4. Enforce custom-slug collision at startup; sidecar-missing-but-hash-ok → write sidecar, don't reinstall.
5. Add `pinmap_missing`, empty-snapshot-with-recipe, and history-pruning policy to §11/§13; fix sidecar fail-closed line.
