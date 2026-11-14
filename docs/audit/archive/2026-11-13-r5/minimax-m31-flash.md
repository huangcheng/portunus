1. **Verdict: GO WITH FIXES — the architecture is viable, but its trust, versioning, and rollout invariants are not yet implementable as written.**

2. **Critical risks**
- **Catalog/server split:** Frozen command/artifact data combined with live manifest/env data can pair an old binary with an incompatible manifest; “vaulted secrets” are ambiguously described as both row and vault data.
- **Installer security:** The runner pin map omits content pins and exact `install_as`; a compromised gateway could alter hashes or retarget an approved script. Both script and runner appear responsible for writing the sidecar.
- **Multi-arch:** The RPC omits `GOOS/GOARCH`; the runner cannot prove that the selected hash belongs to its advertised platform.
- **Migration:** Release A leaves TOML/SQL precedence undefined. Slug-only matching can misbind operator-edited or custom servers.
- **Skill bridge:** The exact dependency matrix between frozen OAuth/command data and live recipe data is missing; manifest-path validation is unspecified.
- **Connect UX:** Connect-time installation, runner selection, site affinity, partial failure, and the 120-second timeout are undefined. Existence-only tree checks also accept stale trees.

3. **Contradictions or stale claims**
- “Before first spawn” conflicts with ensure-on-every-spawn and optional Connect-time ensure.
- Updating the singular runner map invalidates old frozen script/content pins, contradicting “existing servers still ensure.”
- “Satisfied content requires no network” conflicts with missing-sidecar behavior that may rerun a downloading script.
- Section 12 prescribes unit-style tests despite the repository’s E2E-only testing policy.

4. **Missing requirements**
- Exact error bodies/statuses for Connect versus spawn: lock timeout, map mismatch, invalid/missing architecture, runner timeout, sidecar mismatch, and stale tree.
- Exact GET payload: string `id`, whether `slug` is also returned, artifact exposure, 410 response shape, and compatibility for non-SPA clients.
- A server-update whitelist preventing changes to `recipe_slug`, artifact snapshots, and frozen command fields.
- Pin-map versioning, historical-pin retention, rollback, and compatibility with `PORTUNUS_APPS_DIR`.

5. **Architecture/design notes**
The runner—not the gateway—must execute scripts under the installer UID. Use a per-path `flock` plus deterministic global ordering or a runner queue, then reverify after contention. The runner should write the sidecar after successful hash verification, allowing verified airgap installs without network access.

6. **Top 5 concrete edits**
1. Make the runner map exact and versioned: script path/hash, exact `install_as`, and every arch-specific content pin; retain historical versions.
2. Make the runner the sole sidecar writer; atomically replace file/sidecar. Add a tree version marker or weaken the tree-verification claim.
3. Specify the Connect/spawn/site state machine, idempotency, partial-failure recovery, timeouts, and structured errors.
4. Make migration single-source, fingerprint-based, and explicit-map driven, with export, rollback, and rollout ordering.
5. Freeze the API contract and server mutation rules; document exactly which operations require a live recipe.
