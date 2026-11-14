## 1. Verdict
**GO WITH FIXES** — the trust model and error contracts are unusually rigorous, but the airgap rule is self-contradictory, one runner check is not kind-conditional, and Release A's Connect path is underdefined.

## 2. Critical risks
- **Installer security:** solid (pin map allowlist, gateway never ships bytes, sidecar fail-closed, installer uid, same-FS scratch). Residual: unsigned pin map means a compromised *deploy channel* (not gateway) injects pins — correctly flagged, keep it flagged.
- **Multi-arch:** arch is checked at ensure, but nothing requires **arch-aware spawn scheduling**; a flat-pinned recipe on a mixed fleet produces per-spawn `artifact_unsupported_arch` with no hint to pick another site.
- **Migration:** `migration_map.json` + abort-on-unmapped is right, but Release A is ambiguous: a Connect during A resolves the recipe from **SQL**, yet must write a **TOML-derived** snapshot — which source generates `artifact_snapshot` when TOML and SQL disagree?
- **Connect UX:** synchronous ensure up to 120s with lock-wait 30s means Connect can fail while an install is mid-flight; readiness UI deferred to §13 — v1 ships a bad first-run experience with no documented retry guidance.
- **Tree verify:** existence-only means a corrupted/rogue tree passes; §8.3 admits it, but MCP children then exec it — this is the weakest link in §8.2's containment story.

## 3. Contradictions / stale claims
- **Airgap vs fail-closed:** §8.3 "content hash already satisfied → no network" conflicts with step 4 "sidecar missing or disagrees → re-run only if egress allowed." A runner cannot know egress state; pick one behavior.
- §8.1 step 2 demands `content_sha256` unconditionally, but the **tree** RPC carries none — the tuple check must be kind-conditional.
- §4 says Connect sets `servers.slug` "already true today," then makes `recipe_slug` the real key — the dual-column story needs one normative sentence.
- `install_as` uniqueness is startup-checked against **current TOML only**; pin-map **history** can hold two recipes mapping the same `install_as`.

## 4. Missing requirements
- Scratch-dir cleanup after 120s kill (partial state, disk growth).
- "Bake" (history pruning) has no owner or procedure.
- Rollback for Release B step 7.
- Explicit statement that install scripts receive **no vault secrets** (implied by minimal env; say it).

## 5. Architecture notes
Runner executes installs (correct — gateway never ships bytes); trust root = deploy tree + pin map. Locking: `install_as.lock`, flock ≤30s vs install 120s, waiters re-verify — sound; documented timeout mismatch is honest.

## 6. Top 5 edits
1. Rewrite §8.3 step 4: hash-ok + sidecar-missing → attempt re-run; on failure `artifact_install_failed`. Delete "only if egress allowed."
2. Make §8.1 step 2 kind-conditional (tree: `install_as` + prefix only).
3. Add arch-aware site selection to §8.3 or document the cross-arch spawn failure UX.
4. Specify Release A snapshot source of truth + a B cutover rollback.
5. Extend the `install_as` uniqueness invariant to pin-map history; define bake.
