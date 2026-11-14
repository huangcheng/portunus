**1. Verdict:** GO WITH FIXES — the trust model is sound, but snapshot-vs-pin-map drift and the migration classification rule are broken as written.

**2. Critical risks**
- **Snapshot vs pin map deadlock:** runner requires RPC `script_sha256`/`content_sha256` == deploy pin map (built from *live* TOML); servers ensure from a *frozen* `artifact_snapshot`. Any pin update in TOML → every existing server's ensure fails (`artifact_hash_mismatch`/`artifact_path_rejected`) until manual reconnect. No detection, no error code, no refresh path.
- **Migration rule self-contradicts:** §9 step 3 forbids binding "solely because its slug string equals a recipe stem", yet step 4 backfills class (a) — defined *only* by slug-string match. Customs with coinciding slugs get silently bound.
- **tree kind is trust-on-first-deploy:** existence-only check means a wrong/empty `install_as` passes forever; failure only when absent.
- **Lock wait (30s) < install (120s):** first concurrent spawn fails deterministically while another ensure runs — a UX/availability trap, not just a documented footnote.
- **`install_as` collisions:** two recipes (or custom vs catalog) pinning the same path flip-flop installs; no global uniqueness check.

**3. Contradictions / stale claims**
- "New TOML slug colliding with a custom server = ops conflict" but startup only checks *duplicate TOML slugs* — no DB cross-check, no mechanical detection.
- `env_defaults` "must not contain literal secrets" enforced by heuristic `validate_spec` — vague, and "same shapes as today" conflicts with the new rejection rule.
- Sidecar-present-but-hash-mismatch state has no branch in §8.4; "re-run or fail closed" is an unresolved choice, not a contract.
- `id` changes int→string in `GET /api/apps`; same-release 410 noted for mutations but not for this breaking type change.

**4. Missing requirements**
- Error code for stale snapshot; snapshot refresh/reconnect API (reconnect-only is too weak).
- Startup check: recipe `install_as`/`script` uniqueness + collision with existing custom slugs.
- When `artifact_snapshot` writing begins (Release A new Connects?) — unstated.
- Manifest YAML has no integrity pin; custom `skill_bridge` create path unclear (manifest_missing?).
- No test for snapshot↔TOML drift or migration class (a)/(c) edge cases.

**5. Architecture notes**
- Install-on-runner (not gateway) is correct: gateway never ships script bytes; pin map is the trust root. Accept gateway-FS trust for manifests, but say so explicitly.
- flock per `install_as` is right; airgap works only if hash pre-satisfied — document that as a deployment precondition.

**6. Top 5 edits**
1. Add spawn-time reconciliation: snapshot pins ≠ pin map → `artifact_stale_snapshot` + explicit refresh path.
2. Fix §9: class (a) requires provenance evidence (or explicit map), never bare string equality.
3. Startup-validate global `install_as` uniqueness; define every sidecar/hash state with an exact error code.
4. Mechanically detect custom-slug vs new-recipe collision at startup; cover `id` type change in the same-release SPA note.
5. Pin manifest YAML hashes or state gateway-FS trust as a accepted risk; add drift + migration tests.
