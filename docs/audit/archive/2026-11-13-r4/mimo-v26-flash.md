**1. Verdict:** GO WITH FIXES — the split and runner-side install model are sound, but the install_as dual-source-of-truth, arch-hash contradiction, and blind backfill can break live servers.

**2. Critical risks**

- **install_as has two sources of truth:** §4 freezes `install_as` copies into argv at Connect, §8.3 ensures artifacts using live TOML `install_as`. Operator edits the path → installer writes new path, argv points at old → spawn fails or runs stale binary.
- **Blind backfill (§9.3):** `recipe_slug = slug` by equality will mislabel pre-existing *custom* servers that happened to use a catalog stem; their skill_auth then demands a manifest they never had. Reserved-slug rule only guards future creates.
- **Multi-arch:** `content_sha256` is "required" yet `content_by_arch` is offered as an alternative — contradictory. Also unspecified who knows the runner's GOARCH/GOOS; gateway can't pick an arch entry it never learns.
- **Runner trust gap:** runner validates `script` path/hash, but nothing says it validates RPC-supplied `install_as` against the allowed prefixes — a compromised gateway could point the installer uid at arbitrary paths.
- **Installer identity writable dirs + MCP-readable** is right; but no rollback if the script partially writes before failing the final hash check.

**3. Contradictions / stale claims**

- §6 "required" `content_sha256` vs `[artifacts.content_by_arch]` alternative.
- §8.3 "first successful spawn" vs per-site re-ensure on reschedule — ambiguous whether every spawn re-verifies.
- §4 claims tree `path` is "ops docs, not hashed" while drift policy implies trees track TOML — tree drift is undetectable in v1; say so plainly.

**4. Missing requirements**

- Runner-side `install_as` prefix enforcement; sidecar missing/mismatch semantics (pre-v1 installs have no sidecar → forced reinstall? unspecified).
- Overall spawn deadline (30s flock wait + 120s script can exceed any spawn budget).
- Uninstall/cleanup on recipe removal; partial-install rollback.
- SPA behavior if gateway and SPA roll back independently despite "same release."
- Custom `skill_auth` after backfill mislabeling.

**5. Architecture notes**

- Installing on the **runner** (not gateway) is correct: local disk, per-site satisfaction, honest airgap short-circuit via `content_sha256`.
- `flock` + kernel release on death is sound; waiters re-verifying after lock is good.
- Trust root (deploy-pinned script bytes) is a defensible v1 compromise; document that gateway compromise ⇒ arbitrary execution of any deployed script as installer uid.

**6. Top 5 edits**

1. Require exactly one of global `content_sha256` / `content_by_arch` (with fallback); define runner arch reporting in the ensure handshake.
2. Runner validates `install_as` prefix and treats sidecar, not RPC hash alone, as the no-op authority.
3. Pick one install_as owner: freeze it into the server row at Connect, or derive argv at spawn from TOML — state which.
4. Backfill only catalog-created servers (creation-path marker), never blind slug equality; publish the migration SQL.
5. Specify sidecar-missing behavior, global spawn deadline, and an explicit "tree content drift unverified in v1" caveat.
