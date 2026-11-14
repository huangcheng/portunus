**1. Verdict:** GO WITH FIXES — the store split and trust model are sound, but the migration backfill, multi-arch hash default, and runner-side path validation have concrete holes.

**2. Critical risks**

- **Catalog↔server split:** dual `slug`/`recipe_slug` is redundant post-Connect. Worse, §9.3 backfills by "recipe stem" but §5 allows `slug` ≠ filename stem — mis-backfill is possible.
- **Installer security:** §6 enforces `install_as` prefixes and `script` containment *gateway-side at TOML load*. §8.3 runner steps never re-validate `install_as` prefix at RPC time — a compromised gateway can direct writes outside the allowlist.
- **Multi-arch:** single `content_sha256` + a multi-arch script breaks the no-op short-circuit on every non-pinned arch (perpetual reinstall). `content_by_arch` needs the runner's GOARCH, which isn't in the site model.
- **Migration:** servers tied to operator-authored `catalog_apps` recipes get NULL `recipe_slug` and silently lose manifest/`env_defaults`-driven inject behavior (become "Custom").
- **Skill-bridge inject:** TOML-live manifest re-read is fine; but tree verify is existence-only — a stale/wrong-version tree passes.
- **Connect UX:** Connect-time ensure blocks up to 120s × artifacts; the "intended site" at Connect is undefined.

**3. Contradictions / stale claims**

- §11 "missing `PORTUNUS_APPS_DIR` → exit" contradicts §7's packaged-dir default.
- §8.1 RPC marks `script_relpath?`/`script_sha256?` optional; §8.3 treats them as required for `install_script`. State per-kind requiredness.
- §9.4 "do not delete while SPA still reads SQL" is stale once reads cutover to TOML.

**4. Missing requirements**

- Runner-side installers dir configuration; runner enforcement of `install_as` prefix + path containment at RPC time.
- Runner site arch/OS metadata; site-selection policy for Connect ensure.
- Rule: `content_by_arch` required when a script supports >1 arch; dev workflow for recomputing `script_sha256`/`content_sha256` on script edits.
- Sidecar file format unspecified (raw hex vs `sha256sum` line).
- No uninstall/cleanup story for removed recipes or deleted servers.

**5. Architecture/design notes**

Runner executes the script; gateway ships only hashes — correct trust root (deployed disk). flock with post-wait re-verify is right; but 30s wait vs 120s script means concurrent spawns fail spuriously under long installs — document it. Airgap as content-hash short-circuit is honest. Existence-only tree verify trades integrity for deploy simplicity; acceptable for v1 but name the drift risk in ops docs.

**6. Top 5 concrete edits**

1. Backfill `recipe_slug` by matching TOML `slug` values, not filename stems; declare `servers.slug` display-only after migration.
2. Require the runner to independently enforce `install_as` prefix and installers-dir containment per RPC.
3. Add arch/OS to runner site registration; require `content_by_arch` for multi-arch scripts; define Connect site selection.
4. Fix §11: exit only when an explicitly-set `PORTUNUS_APPS_DIR` is empty/unreadable; specify per-kind required RPC fields.
5. In §9, enumerate the silent Custom-fallback for orphaned operator recipes (behavior + data loss) in the export step.
