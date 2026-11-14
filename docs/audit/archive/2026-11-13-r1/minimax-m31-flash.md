1. **Verdict: GO WITH FIXES** — The direction is sound, but the runner trust boundary, recipe versioning, artifact lifecycle, and migration contract must be specified before implementation.

2. **Critical risks**
   - **Catalog↔server split:** `POST /servers` must freeze the recipe revision/spec into the server row; later TOML edits must not silently alter existing servers. Skill-bridge servers still need a stable slug/provenance mapping, so recipe removal or renaming can strand them.
   - **Installer security:** A shipped installer is arbitrary code with runner privileges. Confine paths, reject traversal/symlink attacks, verify a runner-known script digest, use least privilege, and define the trust model for runner hosts versus containers.
   - **Multi-arch:** `PORTUNUS_ARCH` lacks canonical mappings. URLs, SHA-256 values, OS/libc requirements, and unsupported-platform behavior need per-platform declarations.
   - **Migration:** Legacy servers may only reference `catalog_app_id`; no backfill or foreign-key analysis is specified. `catalog_apps` deletion could break skill-bridge operation.
   - **Connect UX:** It is unclear whether Connect waits, returns an installation job, or permits later spawn failure. Progress, retry, timeout, and duplicate-Connect behavior are undefined.

3. **Contradictions or stale claims**
   - Artifacts are described as on-demand, yet `tree` copying is said to be deploy-time. These are materially different models.
   - “Runner executes the script” conflicts with “Gateway (or runner helper),” leaving execution ownership unresolved.
   - An optional `.sha256` sidecar written by the installer is self-attestation, not independent verification.
   - “GET /api/apps unchanged” conflicts with changing IDs to slugs and unspecified 404-versus-removed CRUD behavior.
   - Manifest resolution says `packaged manifests/`, while the repository example uses `packages/portunus_skill_bridge/manifests/`.

4. **Missing requirements**
   - Exact schema validation, missing-directory behavior, and startup error details.
   - API status codes and error bodies for unsafe paths, checksum mismatch, unsupported architecture, lock timeout, timeout/nonzero exit, missing output, and partial installation.
   - Artifact idempotency, staged atomic installation, permissions, rollback, and tree freshness definition.
   - Legacy client compatibility and runner/gateway catalog-version compatibility.

5. **Architecture/design notes**
   - The **runner** should own installation: the gateway sends an artifact ID, expected script digest, and declarative parameters; the runner resolves an allowlisted local script and locks locally.
   - The runner should independently verify the declared expected hash, install via staging plus atomic rename, and persist durable installation state.
   - `tree` artifacts should be baked into or mounted on the runner; gateway-to-runner streaming remains out of scope. Airgapped operation also needs baked binaries or an approved internal mirror, not public downloads.

6. **Top 5 concrete edits**
   1. Specify immutable recipe revisions and the exact catalog-to-server snapshot contract.
   2. Define a runner-owned installer protocol, trust boundary, lock, digest, and atomicity rules.
   3. Split tree and script artifacts into explicit availability models and state machines.
   4. Expand artifact declarations with per-platform URL/checksum metadata and unsupported behavior.
   5. Add a legacy-server backfill, `catalog_apps` dependency analysis, and gateway/runner version-skew policy.
