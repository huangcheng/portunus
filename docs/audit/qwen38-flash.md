**1. Verdict:** GO WITH FIXES — the catalog/DB split and runner-authoritative install are sound, but pin generation, snapshot validation, and the secret-leak rule are under-specified, and migration A→B has a silent-classification hole.

**2. Critical risks**
- Supply-chain concentration: pinned scripts run as the installer uid that *owns every MCP binary*; one hash-matching-but-malicious script poisons all children. No sandbox/namespace.
- `content_sha256` of the *downloaded* binary is a hand-typed TOML value; the real URL lives inside the script, so nothing verifies pin == artifact. Typosquat/typo ships.
- Migration A: catalog Connects only "may" write `recipe_slug`; a server created without it reads as **Custom** in B → silent loss of artifact ensure and live-recipe inject.
- Dual identity `slug` vs `recipe_slug`: startup never checks new TOML slugs against existing DB slugs, so shipping a recipe silently shadows an operator's custom server.
- env_defaults rule rejects only values matching declared secret *names*; a token whose name isn't in `secrets` passes through.
- Connect blocks up to 120s with no async/progress; proxy timeouts unaddressed; concurrent waiters fail `install_lock_timeout`.

**3. Contradictions / stale**
- §7 keeps the `manifest` pointer in overridable `PORTUNUS_APPS_DIR` but the body in the packaged dir → an override yields a dangling pointer caught only at inject, not startup.
- §6's install_script XOR rules aren't applied to §9's backfilled `artifact_snapshot` rows; historical tuples may violate them.
- §8.1 ensure RPC carries a single `content_sha256` while snapshots may store `content_by_arch`; the arch-resolution point is implicit, not stated.

**4. Missing**
- Startup validation of backfilled snapshots; recipe-slug vs DB-slug collision check.
- `install_as` recovery after fail-closed hash mismatch; `history` bake/GC bound (unbounded).
- Runner startup check of `deploy/installers` tree vs pin map (verified only at ensure).
- Error contract + timing for env_defaults violations; parallel-ensure limits; install audit log.

**5. Notes**
The runner (not gateway) executes and writes the sidecar; the gateway only resolves arch and sends tuple refs — keep it that way. Per-`install_as` flock with ≤30s wait is correct; airgap holds only because a satisfied hash short-circuits network, so that path must stay pin-only.

**6. Top 5 edits**
1. At startup, reject TOML slugs colliding with existing `servers.slug`, and validate every backfilled snapshot against §6's XOR rules.
2. Add a CI `--bake` harness that fetches each pinned URL per-arch and emits `content_sha256`; fail on drift.
3. Make `manifest` filename existence a startup check against the packaged dir.
4. Force `recipe_slug` writes on *every* catalog Connect in Release A; forbid NULL on catalog-fingerprinted rows.
5. Tighten env_defaults (reject non-empty values outside an allowlist) and define `portunus artifact reset` as the operator recovery path.
