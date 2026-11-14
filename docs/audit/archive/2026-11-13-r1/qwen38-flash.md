**1. Verdict:** GO WITH FIXES — the TOML/DB split is right, but recipe↔server drift, installer trust, and the undecided install lifecycle block implementation.

**2. Critical risks**
- **catalog↔server:** nothing records which recipe a server came from or its version — §4 omits `server.slug` while §10 assumes it. A TOML `spec` edit silently desyncs existing rows; no re-sync story, undefined behavior when a slug is renamed/removed. Add `servers.recipe_slug` + `recipe_hash`, define drift/orphan semantics.
- **Installer security:** the only integrity guarantee is a hash living *inside* a mutable `.sh`. Nothing signs the script, pins its bytes in TOML, confines `install_as` to an allowed prefix, drops privileges, or restricts egress. Default slug = filename stem invites cross-dir collisions.
- **Multi-arch:** arch/checksum logic duplicated per script, no shared lib, no versioned-path+symlink → no rollback from a bad install. §12 tests fake arch only.
- **Migration:** §9 deletes every `catalog_apps` row in the same release that drops seeding *and* CRUD — no rollback, destroys operator edits made through the old API.
- **skill-bridge inject:** "422 as today" is now unreachable for shipped slugs; it's a startup-known set, so a miss is a programming error.
- **Connect UX:** a ≤120s install inside a Connect POST, with "when the dashboard can wait" as the only plan.

**3. Contradictions / stale**
- §2.5 vs §8.2: `kind="tree"` is declared, yet v1 explicitly cannot install trees at runtime — dead API surface. Drop it.
- §8's trigger ("before first child start … **or** at Connect") is undecided.
- §7.1 "synthetic id = slug … **or** switch SPA to slug keys" unresolved; §10/§12 never pick.
- §4 "labels" vs §6 `label`/`name`; §6's "or skip file with hard error" reopens the refuse-to-start call already made in §5/§11.
- `manifest = "quark.example.yaml"` is a template name, not a packaged path.

**4. Missing requirements**
Runner trust boundary (may runners execute platform scripts, which uid, what control-plane auth, how paths resolve on gateway vs runner); error contracts for install-in-progress, dead lock holder, prefix escape, script-hash mismatch, no-egress; TOML unknown-key policy and `env_defaults`/`secrets` typing; artifact status in health; uninstall/GC.

**5. Notes**
Runner should execute (it owns the target FS); gateway ships content-addressed payloads only. Move pins into TOML — `[artifacts.pins] x86_64 = {url, sha256}` — so scripts become a generic fetch/unpack interpreter: one audited place instead of N. Replace `flock` with a leased lock (`generation` + TTL) and a persisted artifact-result row. Fail fast on airgap rather than timing out.

**6. Top 5 edits**
1. §4/§10: `servers.recipe_slug`+`recipe_hash`; specify drift and orphaned-recipe behavior.
2. §6/§8: arch→{url,sha256} pins and `script_sha256` in TOML; validate `install_as` under an allowlisted prefix.
3. §8: one decision — runner-side async install job, status endpoint, lease lock; delete `tree` from v1.
4. §9: split seed-off / CRUD-off / row-delete across releases; dump rows before deleting.
5. §11: add install-in-progress, lock-timeout, egress-denied, hash-mismatch rows; unknown shipped slug → 5xx.
