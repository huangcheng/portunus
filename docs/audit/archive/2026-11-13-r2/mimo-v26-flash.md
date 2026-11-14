**1. Verdict:** GO WITH FIXES — the ownership split is sound, but the runner↔recipe transport for installer verification and the snapshot-vs-live spec boundary are unspecified enough to cause implementation forks.

**2. Critical risks**
- **Install verification loop:** runner must compare on-disk script bytes against `script_sha256` "from the recipe payload the deploy placed on the runner" — but recipe sync to runners is a non-goal. How (and when) the runner obtains the hash is undefined; this is the security core of §8.
- **Snapshot vs live contradiction:** §4 says spawn/inject read the *current* TOML, while `env_defaults` lives in `[spec]` — is it Connect-snapshot or live? Drift policy explicitly lists `env_defaults` as live "for inject" yet `[spec]` also holds `command`/`args`. One rule must win.
- **Permissions:** scripts run as the MCP child uid, yet targets are `/var/lib/portunus/bin|skills` — writability (pre-chown? per-run uid?) is unstated; likely `spawn_failed` on every fresh host.
- **Connect UX:** synchronous install (120s timeout) inside `POST /servers` risks gateway/SPA timeouts with zero progress feedback; deferring async install doesn't excuse a documented wait bound.
- **Migration:** optional dump + delete of `catalog_apps` silently destroys operator-created recipes; `GET /api/apps` changing `id` from int to string breaks SPA keys unless deployed atomically.

**3. Contradictions / stale claims**
- "No gateway→runner sync" (§3) vs. runner needing recipe payload for hash check (§8).
- Airgap short-circuit: runner verifies script hash *before* running; whether it pre-checks the sidecar first (avoiding script execution entirely) is ambiguous.
- "Stale lock → error" — detection method undefined.
- Filename-stem slug: renaming a file silently breaks server binding; removal is covered, rename isn't.
- Non-goal "no operator overlays" vs. configurable `PORTUNUS_APPS_DIR`.

**4. Missing requirements**
- Transport/authenticity contract for recipe → runner (message shape, when it's fetched).
- `tree` digest: "optional" means existence-only verify — say so normatively.
- Startup validation that skill_bridge `manifest` exists in `packages/.../manifests/`.
- `recipe_missing` scope: only skill-bridge? What about oauth/http recipes edited after Connect?
- Post-deletion path for custom catalog apps (beyond optional dump).

**5. Architecture notes**
- Runner-executed install is correct (filesystem ownership, no root gateway shelling).
- Hash-pinning scripts, not binaries, with in-script per-arch pins: acceptable for v1, but script updates require a TOML hash bump — document the bump procedure.
- flock with waiter re-check is right; specify lock dir and stale detection (PID/age).

**6. Top 5 edits**
1. Define recipe→runner hash delivery explicitly (§8 step 2 + §3 non-goal reconciliation).
2. Make `env_defaults` snapshot-vs-live unambiguous in §4.
3. Add pre-execution sidecar check (airgap no-network) and lock-dir/stale rules.
4. Document deploy-time ownership of `/var/lib/portunus/*` for child uid.
5. Bound Connect's ensure-wait (timeout + "installing…" state) and freeze the `id`=slug API change with a version/atomic deploy note.
