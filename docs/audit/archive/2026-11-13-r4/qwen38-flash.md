**1. Verdict:** GO WITH FIXES — the ownership split is right; the installer trust boundary and the live-manifest/frozen-argv drift are underspecified.

**2. Critical risks**
- **Lock < timeout:** 30s flock wait (§8.3) is shorter than the 120s script timeout, so any spawn burst to one site returns `spawn_failed`. And no failure cooldown: a broken install re-runs on every spawn attempt.
- **Drift:** live manifest at inject + frozen argv has no compatibility check. A recipe change altering required env/flags silently mismatches existing servers. `recipe_rev` (§13) should be v1, not deferred.
- **Hashing model:** `content_sha256` hashes *one file*. Tarball/multi-file tools (rclone unpacks many files) have no artifact kind; "atomic rename into `install_as`" is undefined for directories.
- **Runner-side validation missing:** §8.1 lists only path-exists / sha-match / no-fetch. Nothing re-validates `install_as` on the runner: `…/bin/../../x` passes a string prefix check, and an install_script can write into another recipe's `skills/` tree that §8.3 promises is verify-only. Needs normalization, prefix+symlink checks, and an install audit log.
- **Migration:** §9 never says whether adding `recipe_slug` (3) and mass-deleting `catalog_apps` (4) are one migration. If so, a bad backfill destroys operator rows unrecoverably. Also no uniqueness rule for `slug`, and no handling when a future TOML retroactively reserves an existing custom server's slug.

**3. Contradictions / stale**
- §4 first Binding bullet: Connect sets `servers.slug` "already true today"; the normative bullet uses `recipe_slug`. Two authorities — keep one join key.
- `[artifacts.content_by_arch]` is normative but has no TOML shape in §6, and whether arch is runner-reported or gateway-known is never stated.
- §8.3 short-circuit requires "sidecar agrees"; missing-or-disagreeing sidecar is undefined.
- §4 Binding: "no FK to `catalog_apps`" sits oddly beside §7 still carrying `slug`.

**4. Missing:** runner arch advertisement; `ensure_artifact` protocol-version negotiation for mixed fleets; read-only verify (no way to see unsatisfied artifacts without installing); ordering/partial-failure semantics; script egress/size/disk limits.

**5. Notes:** runner-executes against deploy-published pins is correct. State plainly that installers are a second privileged account running reviewed shell with no fs/network sandbox — that deserves a threat-model section, not a parenthetical.

**6. Top 5 edits**
1. Runner-side `install_as` allowlist enforcement + install audit log.
2. Lock wait ≥ script timeout (or async install with a spawn-blocking state) + failure backoff.
3. `recipe_rev` on the server row, compared at inject.
4. Split migration across two releases; `recipe_slug` as sole join key.
5. Define `content_by_arch` + arch source; add a multi-file kind or drop rclone.
