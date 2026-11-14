1. **Verdict:** GO WITH FIXES — the TOML split and runner-side install model are architecturally sound, but the runner trust boundary and migration edge cases need explicit hardening.

2. **Critical risks:**
- **Gateway→runner script selection:** A compromised gateway can choose *any* deployed script for execution via `ensure_artifact`. The runner validates the chosen script’s hash, but cannot distinguish a legitimate artifact request from a malicious one. A single compromised gateway can turn every runner into an arbitrary-code executor for whatever scripts are already deployed.
- **Migration slug collision:** Existing custom servers may already occupy slugs that later become catalog recipes. The backfill logic does not address this, risking data loss, orphaned customs, or startup failure.
- **Sidecar tampering:** `content_sha256` sidecar is written by the same install process. A compromised runner filesystem can alter both the binary and sidecar, defeating verification. No immutable metadata or signing is specified.
- **30s lock timeout:** Too aggressive for network-bound or cold-start installs; risk of unnecessary `artifact_install_failed` under load or I/O latency.

3. **Contradictions or stale claims:**
- Section 4 treats `servers.slug` and `servers.recipe_slug` as distinct fields, but Connect always sets `servers.slug` from the recipe slug, and custom creates are rejected if they use a reserved slug. Therefore `servers.slug` always equals `recipe_slug` when set; the split adds schema complexity without a defined divergence case.

4. **Missing requirements:**
- Runner subprocess tree kill on 120s timeout (SIGTERM→process group→SIGKILL).
- `ensure_artifact` wire format, versioning, and idempotency key.
- `env_defaults` merge/precedence rules against vault secrets.
- SPA migration path for persisted integer IDs → string slugs.
- Uninstall/rollback path if `content_sha256` matches but the artifact is non-functional.

5. **Architecture/design notes:**
- Runner execution correctly avoids gateway egress and handles multi-arch natively. The dedicated installer UID model is sound, but containerized runners may lack setuid support; specify CAP_SETUID, a sidecar, or user-namespace mapping.
- The dual-hash provenance model (`script_sha256` for deploy script, `content_sha256` for installed artifact) is strong, but both hashes must be verified by the runner *before* execution and *after* install, not just after.

6. **Top 5 concrete edits:**
- **Edit 1:** Collapse `servers.slug` and `servers.recipe_slug` into one field, or document the exact divergence case (e.g., operator override scenario).
- **Edit 2:** Add a runner-side artifact allowlist: the runner maps `artifact_id` to an allowed `script_relpath` from a local manifest; the gateway cannot request arbitrary scripts.
- **Edit 3:** Add a pre-migration scan for slug collisions; require operator rename of conflicting custom servers before backfilling `recipe_slug`.
- **Edit 4:** Define `ensure_artifact` as a versioned, idempotent RPC schema with explicit timeout, runner-auth requirements, and `runner_site_id` scoping.
- **Edit 5:** Specify process-group kill semantics and `stderr_tail` format (e.g., last 4KB or 100 lines, whichever is smaller).
