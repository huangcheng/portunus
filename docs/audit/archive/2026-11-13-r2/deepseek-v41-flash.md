## 1. Verdict
**GO WITH FIXES.** The ownership split and the 410 cutover are right, but the installer trust chain self-certifies and the gateway/runner execution boundary is undefined, so §8 is not implementable as written.

## 2. Critical risks
- **Circular integrity.** The script *writes* `install_as.sha256`; the runner then "verifies" that sidecar against the installed object. That proves self-consistency, not provenance — a mis-pinned script certifies its own payload. With `[artifacts.pins]` deferred and `script_sha256` compared against a copy from the same deploy, nothing binds installed bytes to a reviewed value. This *is* the security model.
- **No executor.** §8 says runner, "gateway never shells out"; §7 has Connect — a gateway handler — "call the same ensure-path and wait". No runner RPC, auth, or payload is specified.
- **`servers.slug` is a string, not a binding.** A custom server named `dropbox` is silently treated as a shipped app, inheriting install gates and recipe inject.
- **Live TOML mutates running servers.** §4 reads *current* `manifest`/`env_defaults` on every inject, with no `recipe_version` — the "Connect-time snapshot" guarantee is false for manifests.
- **One malformed file refuses startup**, taking control of unrelated apps down with it.
- **Privilege:** children share the uid that can write `/var/lib/portunus/bin/`, so a hostile MCP child can overwrite `rclone`.

## 3. Contradictions / stale claims
- Snapshot (§4) vs live `env_defaults`.
- §2.5 "deploy-guaranteed, verify at runtime" vs §8 tree failure → `spawn_failed`; digest is "optional" for code a skill-bridge CLI executes.
- §8 airgap returns `artifact_install_failed`; §11 says "(or `spawn_failed`)". Pick one.
- §6 validates `script` paths only — `spec.manifest` existence unvalidated, so failure lands at first inject, not startup.
- §9.4 export "optional" + §9.3 "no new column" ⇒ operator CRUD edits lost by default; SPA `id` migration assumed free.

## 4. Missing requirements
- Runner RPC (`ensure_artifacts`): auth, idempotency, 202-vs-blocking Connect, whether the server row persists on artifact failure.
- `servers.origin` + `recipe_version`; repair path for renamed/removed slugs.
- Symlink-safe `install_as` (realpath-under-prefix, `O_NOFOLLOW`); duplicate `install_as` across recipes → startup error.
- How `deploy/installers/` and `third_party/` reach *remote* runners given no control-plane sync (explicit non-goal). Manual provisioning is load-bearing and undocumented.
- SPA-renderable error codes: stale-lock vs timeout, unsupported arch, sidecar missing, egress-denied vs slow (120s blackhole × artifacts = hung Connect).
- `GET /api/apps` schema, 410 body shape, string `id` breaking int-keyed clients.

## 5. Architecture notes
Runner-executes is correct. `flock` releases on process death, so "stale lock" must be PID+start-time in the lock file, not file existence; the waiter's post-lock re-check is meaningless until the expected hash comes from TOML, not the script. Probe egress with a ~5s connect timeout and a distinct offline code, separate from the 120s job timeout.

## 6. Top 5 concrete edits
1. `expected_sha256` per arch in TOML; runner verifies the installed object against TOML. Sidecar becomes a cache.
2. Specify the runner RPC: Connect inserts `pending`, calls ensure, returns 202 + status.
3. Add `origin` + `recipe_version`; quarantine bad recipes (log, drop card, `recipe_invalid` on Connect) instead of exiting; CI-lint TOML, hash freshness, duplicate `install_as`.
4. Document per-runner provisioning of installers and trees (hash-verified, idempotent) — not deferrable.
5. Full error table, symlink-safe `install_as`, 5s offline probe.
