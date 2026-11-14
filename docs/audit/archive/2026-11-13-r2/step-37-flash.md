**Verdict:** GO WITH FIXES — the recipe/DB split is correct, but the runner install handshake, fleet locking semantics, and script distribution model are incomplete and would block a multi-runner deployment.

**Critical risks**
- **Runner trust boundary:** Section 8 assigns install execution to the runner, yet omits the runner<->gateway RPC used to trigger it. Without this, the design is not implementable across a fleet.
- **Script distribution gap:** Runners must already have `deploy/installers/*.sh` to verify `script_sha256`, but section 3 lists control-plane blob sync as a non-goal. How scripts reach runners is undefined.
- **Fleet locking:** `flock install_as.lock` is local to one runner. If the same slug spawns on N runners, concurrent installs are not prevented.

**Contradictions / stale claims**
- Section 8 says the runner verifies the script hash using the recipe payload “the deploy placed on the runner,” but section 7 restricts TOML loading to the gateway process. The runner either needs the recipe (implying an unstated protocol) or the gateway must mediate verification.
- Section 3 defers `[artifacts.pins]`, yet section 8 requires install scripts to “detect arch itself; fetch pinned URL+SHA256.” Without pins in TOML, arch logic is hardcoded in shell scripts, defeating recipe-driven updates.

**Missing requirements**
- Runner<->gateway artifact protocol: message schema, streaming, redaction, timeout.
- Runner bootstrap model for `deploy/installers/` (baked image / AMI / one-time sync?).
- Locking scope (local vs distributed) and behavior under multi-runner concurrency.
- Vault secret injection for installers: section 8’s env allowlist has no credential path, which breaks real-world installers.
- TOML schema versioning / evolution policy (no equivalent of DB migrations).

**Architecture / design notes**
- Gateway should mediate installs: send `ArtifactEnsureRequest(slug, artifact_id, expected_sha256)` to the runner; the runner compares its local script bytes, runs under `flock`, and returns a structured result. This closes the gateway/runner recipe asymmetry.
- Use per-runner atomic install dirs (`.installs/<slug>/<artifact-id>`) with per-artifact locks rather than a single `install_as.lock`, avoiding cross-artifact contention.
- Airgap behavior should be enforced in the script (exit code + sidecar write) so the runner can distinguish “already installed” from “offline, cannot fetch.”

**Top 5 concrete edits**
1. Add a `RunnerArtifactClient` section defining the request/response schema between gateway and runner for artifact ensure-path.
2. In section 8, replace `install_as.lock` with per-artifact lock files in a `.installs/` staging dir; document that N runners install independently (no global lock).
3. Define how `deploy/installers/` reaches runners (e.g., baked into runner image or one-time bootstrap sync) before first spawn.
4. Extend the env allowlist with a vault-keys injection mechanism (`PORTUNUS_VAULT_KEYS` or secret-file path) so installers can access needed credentials.
5. Add `x-schema-version = 1` to the TOML schema and a startup rule that rejects unknown top-level keys, enabling safe recipe format evolution.
