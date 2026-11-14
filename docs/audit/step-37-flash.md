1. **Verdict:** GO WITH FIXES — sound architecture, but the installer supply-chain trust model, migration pin-history backfill, and error completeness need hardening before implementation.

2. **Critical risks:**
   - **Deploy-channel supply chain:** The runner validates script bytes against a local pin map built at deploy time. If the deploy channel is compromised, an attacker can plant a malicious script and matching hash; the runner will install it without complaint. The design treats this as an "ops trust assumption" but provides no mechanism (signed pin map, immutable runner image, hash-of-hash) to detect compromise.
   - **Migration A mass-reinstall:** §9.4 backfills snapshots from current TOML only. §6 history retains old tuples for *new* snapshots, but backfilled servers carry no history. The first post-migration TOML pin change triggers `artifact_stale_snapshot` on every migrated server, forcing mass reinstall or manual reconnect.
   - **Skill-bridge inject has no corrupt-manifest error:** If the manifest YAML is present but malformed, the design has no error code. `manifest_missing` does not cover corrupt data, risking unhandled exceptions at spawn time.

3. **Contradictions or stale claims:**
   - §2 describes arch-aware installers but does not explicitly permit or forbid single-arch flat `content_sha256`. §6 mandates `content_by_arch` for multi-arch but never clarifies whether single-arch flat pins are valid in v1, leaving validators ambiguous.
   - §8.1 claims a compromised gateway is limited to the deploy pin map, yet §8.2 grants the install script a dedicated installer uid with write access to `/var/lib/portunus/bin`. A compromised gateway can still force reinstallation or downgrade of any pinned artifact, causing supply-chain substitution or DoS. The sentence understates residual risk.

4. **Missing requirements:**
   - **Pin map bake lifecycle:** Who prunes `history`, and via what API/CLI? Without this, the allowlist grows unboundedly.
   - **`manifest_invalid` error code** for parseable-but-bad YAML.
   - **Migration pre-flight remediation:** §9 mentions a warning if on-disk hashes don't match pins, but not the forced-reinstall or allowlist path.
   - **Runner concurrency model:** Does the runner serialize all `ensure_artifact` calls per site, or can different artifacts install in parallel?

5. **Architecture/design notes:**
   - **Runner-side execution is correct:** the dedicated installer uid + `flock` + atomic rename isolates install from MCP child processes.
   - **Gateway-side inject is correct:** vault secrets never touch the runner; env injection stays in the gateway.
   - **Fail-closed hashing:** requiring the runner to verify the sidecar before declaring success prevents half-written binaries from being used.

6. **Top 5 concrete edits:**
   1. **§9:** Backfill `history` in the runner pin map during migration A, populating it with the pre-migration tuples for every backfilled server so existing snapshots survive the first TOML update.
   2. **§11:** Add `manifest_invalid` to the error table, covering malformed YAML at inject time.
   3. **§8.1:** Replace the "ops trust assumption" with an immutable pin-map delivery requirement (read-only deploy partition or image layer), or add a signed-pin-map validation step.
   4. **§6 / §8.1:** Define a `portunus pin bake` command (or API) that atomically prunes `history` after operator confirmation.
   5. **§8.3:** Explicitly state whether `ensure_artifact` calls for different artifacts on the same runner are serialized or parallel, and what locks apply.
