**1. Verdict** — GO WITH FIXES: the ownership split and runner-side installer model are sound, but the runner payload contract, migration data-loss path, and installer environment need tightening first.

**2. Critical risks**
- **Runner trust boundary:** install scripts run as the MCP-child uid but must write `/var/lib/portunus/bin/`. If that dir is MCP-uid-writable, any spawned child can swap binaries — de-facto root for the skill fleet. Specify dir ownership/permissions or a privileged helper.
- **Script-hash provenance:** §8 verifies on-disk bytes against "the recipe payload the deploy placed on the runner," yet §3 defers gateway→runner sync. Who ships the hash, and what happens on gateway/runner commit skew? Every spawn fails on hash mismatch until redeploy; also no re-hash at exec time (TOCTOU).
- **Migration:** §9.4 makes export *optional* but row deletion unconditional — guaranteed data loss for operators who skip it.
- **Env allowlist is unrunnable:** curl/sha256sum/tar need `PATH`; the literal three-variable list breaks every shipped script.
- **Slug rename** (filename change) silently demotes live servers to "Custom" with no drift signal.

**3. Contradictions / stale claims**
- §3 "no gateway→runner blob sync" vs §8's deploy-placed recipe payload — sync exists implicitly, unspecified.
- §9: optional export + mandatory delete.
- §4 says servers keep running on snapshot after slug removal, but §8 then skips artifacts for unknown slugs — a broken tree install becomes invisible rather than failing loudly.

**4. Missing requirements**
- TOML strictness: unknown keys — refuse to start or warn?
- Stale-lock detection: `flock` self-releases on process death; "stale lock" heuristic undefined.
- Tree digest: algorithm, who computes the baseline, and why it's optional when tamper-evidence is the stated point.
- Hard egress/connect timeout for the airgap fail-fast (not "no long hang").
- Install audit log (trigger, artifact id, hash used).

**5. Architecture notes**
- Runner executes installers — correct; the gateway must never shell remote hosts. Keep.
- `flock` per `install_as` with post-lock hash re-check is right; 30s wait already specified.
- Airgap policy is right in principle; it needs a numeric timeout to be enforceable.

**6. Top 5 edits**
1. §9.4: make export a hard prerequisite for deleting `catalog_apps` rows.
2. §8: define the gateway→runner payload (script path, `script_sha256`, recipe/commit id) and require re-hashing immediately before exec.
3. §6/§8: minimum env = `PATH`, `TMPDIR` (+ script-declared extras); specify `/var/lib/portunus/bin` ownership and mode.
4. §6: state strict-vs-lenient unknown-key policy; document `slug`-pinning guidance to survive file renames.
5. §6: make tree digest mandatory, name the algorithm, and assign baseline computation to deploy.
