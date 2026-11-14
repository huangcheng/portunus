**1. Verdict:** GO WITH FIXES — the store split and file-recipe approach is right, but the runner trust boundary, per-arch content pins, and the `servers.slug` backfill will bite in production.

**2. Critical risks**
- **Runner never validates `install_as`.** Only the gateway does (§6); §8.1 lists it as a runner input, not a checked one. A compromised gateway sends `install_as=/etc/cron.d/x` with a genuinely deployed script hash → installer-uid write outside `/var/lib/portunus/{bin,skills}`. §8.1's claim ("can only request execution of an already-deployed script") understates it: that script runs with attacker-chosen env and target.
- **Backfill silently demotes to Custom.** §9.3 backfills only where `slug` equals a TOML stem. If today's `servers.slug` holds a `catalog_apps` id or a label, `recipe_slug` stays NULL → artifacts skipped, skill_auth falls back to vault → silent breakage labelled "Custom", worse than Orphaned.
- **One `content_sha256` cannot describe arch-varying binaries.** §6 makes it required; the per-arch table is "optional"; §8.1's RPC field is flat.
- **Connect blocks up to 120s × N artifacts** through an HTTP request, with no defined server state on failure.
- **Every spawn of an installed artifact pays a full-file sha256 RPC round trip** — no per-site satisfaction cache.

**3. Contradictions / stale claims**
- §7 catalog is in-memory at startup, but §4 says `manifest` is "read at inject time… not cached for the life of the process". TOML edits need a restart.
- §8.3 hashes `install_as` for both kinds; a `tree` is a directory — sha256 undefined. §6 says trees are "not hashed in v1".
- §5 defaults `PORTUNUS_APPS_DIR` to the packaged dir; §11 makes an empty/missing dir a startup exit. Also unclear whether the env var *replaces* or *merges* the shipped set (replace silently deletes Connect cards).
- §6 requires `content_sha256`; the comment beside the same field makes `content_by_arch` the alternative.

**4. Missing requirements**
- Runner-side `install_as` prefix allowlist + rejection error code; lock keyed per `install_as`, not per artifact.
- Exit-code contract so "unsupported arch" ≠ `artifact_install_failed` (stderr_tail alone isn't machine-readable).
- Server state machine after a failed Connect-time ensure (no async job ⇒ undefined; does retry re-run installers?).
- `content_by_arch` key format (`linux/amd64`) and a startup check that every declared arch is covered.
- Tooling to compute/verify `script_sha256`/`content_sha256` — otherwise pins rot on the first script edit.
- Reserved-slug policy: adding a recipe later whose slug an operator already used for a custom server is undefined.
- Kill semantics: timeout must kill the process group, then re-verify — atomic rename alone doesn't cover a half-written scratch.

**5. Design notes**
Runner executes (correct — gateway can't reach the fs). Lock file must live in the installer-writable dir and survive leftovers; rename must be same-directory. Airgap = hash short-circuit only — true, but the ensure RPC itself still needs the control channel.

**6. Top 5 edits**
1. §8.1/8.3: runner independently validates `script_relpath` and `install_as` against prefixes; new error `artifact_path_rejected`.
2. §9.3: pre-migration audit dumping DISTINCT `servers.slug` without a stem match; require an explicit map, fail startup if unmapped.
3. §6: exactly one of `content_sha256` XOR complete `content_by_arch`, with GOOS/GOARCH coverage validated at startup.
4. §4/§7: restate TOML as startup-immutable, add SIGHUP reload; only manifest YAML read fresh.
5. Add exit-code convention (78 = unsupported arch) and `portunus catalog verify` recomputing both pins.
