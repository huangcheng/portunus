1. **Verdict: NO-GO — the core catalog/live-state split is viable, but installer trust, multi-arch pinning, migration provenance, and manifest delivery remain implementation-blocking ambiguities.**

2. **Critical risks**
- **Catalog ↔ server:** Artifact declarations and hashes are live from TOML, while `install_as` is frozen in server argv. Changing an artifact path can install a new executable while the server still invokes the old path.
- **Installer security:** The gateway chooses script, hash, artifact ID, and destination. A compromised gateway could execute any deployed script under the installer identity. The runner needs a local allowlist mapping artifact IDs to script paths, hashes, and destinations.
- **Multi-arch:** One `content_sha256` cannot validate different amd64/arm64 outputs. The optional arch table lacks a valid, unambiguous TOML location and runner platform handshake.
- **Migration:** Backfilling by matching `servers.slug` can misclassify a custom server as catalog-backed. Removing Catalog Config also silently breaks operator-authored recipes unless export or migration is mandatory.
- **Skill-bridge inject:** It is unclear whether the gateway or runner reads manifests, where that package exists on runners, and how manifest-name containment is enforced.
- **Connect UX:** “Connected” does not imply artifact-ready on the eventual runner site. The UI/API need readiness, failed-site selection, and retry semantics.

3. **Contradictions or stale claims**
- `PORTUNUS_APPS_DIR` is unset-by-default, but §11 says missing or empty exits; only empty or nonexistent explicitly configured directories should fail.
- `content_sha256` is both mandatory and replaceable by per-arch pins.
- TOML uses `script`, while the RPC uses `script_relpath`; their roots must be defined to prevent `deploy/installers/deploy/installers/...`.
- Live artifact paths conflict with frozen argv paths.

4. **Missing requirements**
- Independent runner-side path, symlink, ownership, and destination-prefix checks.
- Stable error codes for tree absence, lock timeout, immutable `recipe_slug`, manifest-read failure, and migration failure.
- Exact Connect request semantics and API schema for string `id`/slug.
- Startup/deployment verification that local script bytes match TOML pins.

5. **Architecture/design notes**
The runner—not the gateway—must execute installers on the target site, under a dedicated UID. Serialize per-destination installs with kernel `flock`, reverify hashes while holding the lock, and short-circuit before network access. Airgap only works when the pinned artifact is already installed; otherwise installation must fail within the timeout.

6. **Top 5 concrete edits**
1. Define platform-keyed artifact hashes and runner platform reporting.
2. Add runner-local artifact/script/destination allowlists and safe-path rules.
3. Freeze an artifact snapshot at Connect, or consistently use live paths.
4. Replace slug-only backfill with provenance mapping and mandatory export.
5. Specify manifest deployment/source and revise SPA/API/error contracts.
