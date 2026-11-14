**1. Verdict**  
GO WITH FIXES — sound TOML-first architecture, but installer trust, migration safety, and silent tree drift must be resolved.

**2. Critical risks**  
- **Runner pin map lacks a contract.** No schema, build pipeline, or bit-identical distribution guarantee is specified. A compromised gateway could request arbitrary paths if the map diverges across the fleet.  
- **Tree artifacts drift silently.** §8.1 accepts “content drift undetectable — ops must deploy the right tree”; tampered skill trees are never audited.  
- **Migration step 3 is unsafe.** It classifies `servers.slug` against TOML slugs, then backfills `recipe_slug`. A custom server named `quark` predating the recipe is misclassified unless the operator supplies an explicit map, and the map format is undefined.  
- **Synchronous install = hard spawn failure.** 30 s flock + 120 s timeout; no retry or async path.  
- **`env_defaults` filter is unimplementable.** “Secret-looking” has no criteria, so `validate_spec` cannot be enforced consistently.

**3. Contradictions / stale claims**  
- Non-goal defers `[artifacts.pins]` URL table to v1, forcing binary provenance into opaque shell scripts and undermining TOML auditability.  
- “Runners never read TOML” holds for inject, but the spec omits whether runners need TOML access for artifact ensure (they do not).

**4. Missing requirements**  
- Runner pin map file format and deployment contract.  
- Migration map schema for unmapped slugs.  
- `validate_spec` rules for `env_defaults`.  
- Failed ensure state, retry API, and spawn behavior.  
- Runner site registration payload definition for `GOOS`/`GOARCH`.

**5. Architecture / design notes**  
- The gateway→runner control channel is the trust root, but its auth model (mTLS vs runner token) is mentioned only in passing and not tied to pin-map validation.  
- Install scripts are now the most security-critical deployable artifact; any binary update requires a script deploy, not a TOML edit. Acceptable, but should be explicit in release docs.  
- `flock` is fine, but synchronous spawn means transient failures become spawn failures. If kept synchronous, make the timeout configurable.

**6. Top 5 concrete edits**  
1. Define a runner pin map file (`artifact_id → script_relpath, script_sha256, install_as, allowed_prefix`); runner validates it at startup and rejects unknown IDs.  
2. Hash tree artifacts (tar + SHA256) and verify at ensure time; remove the drift-undetectable carve-out.  
3. Replace migration step 3 with a normative `migration_map.json` (`old_slug` → `recipe_slug | "custom" | "unmapped"`), require operator sign-off, and abort on unmapped.  
4. Add `POST /servers/{id}/ensure` and `last_ensure_status` so transient failures are retryable without reconnect.  
5. Require install scripts to verify the downloaded file hash before rename, and require the runner to atomically check `sha256(install_as) == content_sha256` and write the sidecar under the same lock.
