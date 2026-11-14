# Portunus TOML Catalog — Audit

**1. Verdict — GO WITH FIXES.** Sound trust boundary and snapshot model, but one internal contradiction, ambiguous migration classification, and missing retry/pin-map error contracts.

**2. Critical risks**
- **Catalog↔server split:** migration step 3 requires classifying slugs (a)/(b)/(c) but gives no objective test separating a catalog Connect from a custom server that happens to share a slug — precisely the binding the spec forbids.
- **Installer security:** the deploy pin map is unsigned; compromising the deploy channel (rsync/image) yields arbitrary runner code. Threat model covers gateway compromise only.
- **Locking:** 30s flock wait vs 120s install means concurrent Connect/spawn fail spuriously as `spawn_failed`; no retry/backoff → every spawn re-hammers a broken runner.
- **Multi-arch:** flat `content_sha256` carries no arch; on a mismatched-arch runner the script installs wrong content → `artifact_install_failed`, never `artifact_unsupported_arch`.
- **Connect UX:** synchronous ensure can block ~120s or fail on contention with no readiness feedback (acknowledged but unresolved).

**3. Contradictions / stale claims**
- §7 "recipe metadata is startup-immutable; only manifest YAML bodies re-read" vs §12 test "change TOML manifest pointer → next inject sees new manifest". Pick one.
- §7: `PORTUNUS_APPS_DIR` "replaces" the packaged set — unspecified whether manifests resolve from the custom dir or the packaged manifests dir.
- §6: hash-XOR startup failure ("both/neither/empty → exit") is not scoped to `kind = "install_script"`; tree artifacts carry no content hash.

**4. Missing requirements**
- `artifact_path_rejected` conflates unknown pin with path escape; add `artifact_pin_missing`.
- Sidecar write ordering/atomicity after rename; scratch cleanup on SIGKILL.
- Invalidation rule for the per-site "last good ensure" cache (pin-map change? runner image update?).
- SPA cutover: how existing integer server ids key the connected tab after `GET /api/apps` becomes slug-keyed.
- OS scope: `.sh` scripts + GOOS/GOARCH imply Linux-only runners — unstated.
- No ensure audit log / metrics requirement for fleet ops.

**5. Architecture notes**
Runner-side execution is correct: gateway never ships script bytes; the runner validates against the deploy pin map; the installer uid separates privileges from MCP children; flock's kernel-owned lifetime is sound. Airgap depends on the hash short-circuit; URL honesty stays inside reviewed scripts. Weakness: trust reduces entirely to the deploy channel — sign the pin map now (cheap) rather than deferring alongside signed RPCs.

**6. Top 5 edits**
1. Fix §7/§12: make the manifest pointer hot-readable at inject, or change the drift test to require a restart.
2. Define manifest resolution when `PORTUNUS_APPS_DIR` is set.
3. Scope the hash-XOR rule to `install_script`; add explicit mismatched-arch rejection for flat pins.
4. Split `artifact_pin_missing` from `artifact_path_rejected`; define retry/backoff policy for `artifact_install_failed`.
5. Migration: specify objective (a)-vs-(b) criteria (match argv/url/oauth/spec shape, never slug string alone); require a map file for every non-conforming row.
