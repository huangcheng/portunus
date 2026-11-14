1. **Verdict:** GO WITH FIXES — the TOML split removes DB drift and simplifies the control plane, but runner provenance and artifact contracts are under-specified.

2. **Critical risks:**
   - **Runner trust boundary:** the runner verifies script hash from the gateway RPC, not from a local TOML or signature. A compromised control plane can send a hash for a script that exists on disk but was not reviewed in that state; the runner has no independent pin to reject it. The deploy→runner trust needs a locally-verifiable anchor.
   - **Manifest drift ambiguity:** section 4 says the manifest body is live from TOML, but section 7 says `AppCatalog` is loaded at startup. It is unclear whether the gateway re-reads the manifest YAML at every inject or caches it, which affects whether a manifest edit takes effect without restart.
   - **Migration cutover:** removing `ensure_defaults` and all `catalog_apps` reads must be atomic. With no SQL fallback, a partial deploy leaves servers orphaned mid-rolling update.

3. **Contradictions or stale claims:**
   - Section 6 lists `/var/lib/portunus/skill-state/` as an `install_as` allowlist prefix; section 8.2 does not list it as installer-writable.
   - Section 4 (live manifest body) vs section 7 (startup-only load) implies the manifest YAML may be stale if cached.

4. **Missing requirements:**
   - `tree_digest` field in the `[[artifacts]]` schema (referenced in the RPC and runtime section, absent from schema).
   - `manifest_missing` error code (only `recipe_missing` exists; a valid TOML pointing to a missing manifest file is unhandled).
   - Install script audit contract: stdout/stderr handling, exit code, duration, redaction policy.
   - Slug normalization rules (case, allowed characters) and behavior for non-`.toml` files in `PORTUNUS_APPS_DIR`.
   - Behavior when `PORTUNUS_APPS_DIR` is missing or empty.

5. **Architecture/design notes:**
   - Gateway (control plane) vs runner (execution plane) split is correct. `flock` is acceptable for single-host runners; document that multi-host fleets need a distributed lock (out of scope for v1).
   - Airgap model is sound: the runner never fetches scripts, only validates pre-deployed bytes and caches results via sidecar.

6. **Top 5 concrete edits:**
   1. Add `tree_digest = "<hex>"` to the `[[artifacts]]` schema for `kind = "tree"` and validate it at gateway startup.
   2. Clarify manifest caching policy: explicitly state whether manifest YAML is read at inject time or cached at startup, and add `manifest_missing` to the error contract.
   3. Define structured install-script audit logging: return `{artifact_id, exit_code, duration_ms, stderr_tail}` to the gateway; discard stdout unless explicitly needed.
   4. Harden runner provenance: ship the TOML pins to runners (or sign RPCs) so the runner does not blindly trust the gateway’s `script_sha256`.
   5. Add startup guardrails: fail if `PORTUNUS_APPS_DIR` is missing, normalize slugs to lowercase, and reject non-`.toml` files in the apps directory.
