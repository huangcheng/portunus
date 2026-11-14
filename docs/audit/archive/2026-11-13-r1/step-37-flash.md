**1. Verdict:** GO WITH FIXES — the TOML split is architecturally sound, but runner trust, migration FK safety, and operator CRUD removal need explicit guardrails.

**2. Critical risks**
- **Skill-bridge inject regression:** `registry._inject_skill_bridge_manifest` currently reads from `catalog_apps`. Switching to TOML-by-slug breaks custom skill-bridge servers (no recipe), and the spec does not define a fallback to the server row.
- **Runner trust boundary:** §8 grants the runner arbitrary shell execution. If runners are remote or multi-tenant, this is a supply-chain takeover. Even locally, no capability drop is mandated.
- **catalog_apps migration FK hazard:** §9 deletes all rows, but if `servers` or `grants` holds a FK to `catalog_apps.id`, the migration fails or orphans data. No FK audit is listed.
- **Operator UX hole:** §4 removes Catalog Config CRUD. Operators managing custom recipes via the UI lose their workflow with no v1 replacement.
- **Locking scope:** `flock` on `install_as.lock` assumes shared filesystem. In a multi-host fleet, lock files are per-host; concurrent installs still race.

**3. Contradictions or stale claims**
- §8 says "Gateway (or runner helper) evaluates," but §6 says "runner executes." The orchestrator is undefined.
- §5 lists `third_party/` as "copyable trees," yet §8.2 says v1 requires trees on the runner image/volume. Conflicting runtime models.
- §2 Goal 5 says skill trees remain copyable, but §8.2 implies deploy-time baking. If baked, they are no longer runtime-copyable.

**4. Missing requirements**
- **Script sandbox:** No capability restriction, network egress policy, or filesystem view bounds beyond path allowlisting.
- **Contention semantics:** No timeout for waiters, no handling of hung scripts.
- **Rollback/atomicity:** Partial installs (binary present, symlink missing) are unhandled.
- **SPA id strategy:** §7 leaves it unresolved; frontend and API must pick one.
- **Backfill:** Existing `servers` rows need `slug` to render in the Connected tab if the UI joins by slug.

**5. Architecture/design notes**
- Define a single Install Coordinator. Recommendation: gateway dispatches install jobs to the assigned runner; runner executes locally and streams logs/status back. This avoids cross-host `flock`.
- Airgap: install scripts must support a local cache or offline mode now, not as a follow-up.
- Consider code signing for install scripts or their artifacts if runners cannot be fully trusted.

**6. Top 5 concrete edits**
1. Add a "Runner Trust Boundary" subsection declaring runners trusted or sandboxed, and mandate capability dropping for install scripts.
2. Replace `flock` with runner-local locking or gateway-coordinated leases; add a 300s contention timeout with explicit failure.
3. In the migration plan, add a pre-migration FK audit of `catalog_apps`; either drop FKs or retain the table stub through the bake period.
4. Add `version = 1` to the TOML schema and fail on unknown top-level keys to prevent silent drift.
5. Adopt `slug` as the public API id now, and backfill `servers.slug` from TOML during migration.
