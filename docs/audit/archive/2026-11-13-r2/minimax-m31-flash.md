1. **Verdict: GO WITH FIXES** — The direction is coherent, but runner-side artifact execution, trust/versioning, and migration need explicit contracts before implementation.

2. **Critical risks**
   - **Catalog/server split:** Existing servers keep command snapshots but consume the current manifest; a recipe edit can pair a new manifest with an old CLI. A removed recipe makes a platform server look “Custom,” and custom slugs can collide with future recipes.
   - **Installer security:** A checksum authenticates only relative to an already trusted recipe/script pair. Prefix checks must handle symlinks and `..`; a non-root child UID may not write the default paths. Sidecars do not prove architectural usability.
   - **Multi-arch:** Self-detection is insufficient without a supported OS/architecture matrix, libc rules, wrong-architecture detection, and pinned outputs.
   - **Migration/skill bridge/UX:** Deleting rows breaks operator recipes and rolling deployments; inject may precede artifact installation; Connect’s payload, timeout behavior, and first-use install state are unspecified.

3. **Contradictions or stale claims**
   - Trees may have “optional digest checks,” yet no digest field or source of truth exists.
   - The runner supposedly receives checksummed “recipe payload,” although script/recipe synchronization and version skew are undefined.
   - “First successful spawn” does not cover later artifact additions; “Connect may … ensure” is non-normative.
   - Custom `skill_bridge` `skill_auth` is promised without defining how a custom server obtains a manifest.

4. **Missing requirements**
   - Runner RPC/API, authentication, per-runner artifact state, idempotency, cancellation, process-group timeout, and stable error fields/code.
   - Canonical path/symlink checks, destination ownership/modes, writable non-root directories, scratch cleanup, and controlled `PATH`.
   - Strict TOML validation: required fields, unknown keys, artifact-ID/install-path collisions, and recipe/script revision compatibility.
   - Rolling migration/rollback plan and complete Connect/SPA request-response contract.

5. **Architecture/design notes**
   The gateway should resolve/validate recipes; the runner should execute only platform-deployed, locally pinned scripts, using host-local locking and verifying output after installation. Recipe and script revisions must be co-deployed, or the runner must reject stale requests. Egress and airgap behavior belong to the runner; the gateway should never proxy arbitrary script bodies. A trusted script may contain binary pins, but its output digest should remain independently checkable.

6. **Top 5 concrete edits**
   1. Specify the runner artifact-ensure protocol and revision/trust handshake.
   2. Add tree/output digests, canonical path rules, permissions model, and architecture matrix.
   3. Version manifests/artifacts or enforce compatibility before current-recipe inject.
   4. Make migration phased: inventory, mandatory export, compatibility window, bake, then deletion.
   5. Define Connect/API errors and guarantee artifact ensure before inject/spawn.
