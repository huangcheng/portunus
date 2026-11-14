## 1. Verdict

**GO WITH FIXES** — the catalog/server split is viable, but installer enforcement, migration atomicity, and runtime contracts remain insufficiently specified.

## 2. Critical risks

- **Catalog↔server split:** Live manifest/`env_defaults` versus frozen command/artifact snapshots can drift; orphan detection and future catalog/custom slug collisions are not mechanically enforced.
- **Installer security:** The runner pin map is the trust root; hash-check/exec TOCTOU remains, a dedicated installer UID is not a sandbox, and downloaded URLs or stderr may leak credentials.
- **Multi-arch:** GOOS/GOARCH advertisement freshness, old-runner capability negotiation, and exact per-architecture tuple matching are undefined.
- **Migration off `catalog_apps`:** Release A has two possible authorities; concurrent creates, migration-map approval, export format, and atomic backfill are unspecified.
- **Skill-bridge inject:** Re-reading mutable YAML permits partial deployments and has no malformed-manifest error contract.
- **Connect UX:** No rule selects a runner site, orders installation versus OAuth, or presents synchronous waiting and failure.

## 3. Contradictions or stale claims

- Release A calls SQL authoritative while new Connects may snapshot TOML, without defining precedence.
- Airgap says a satisfied content hash requires no network, but a missing/disagreeing sidecar forces script rerun even when the installed hash matches.
- Tree verification occurs during ensure, while the error table reports tree absence as `spawn_failed`.
- Snapshots support `content_by_arch`, but `ensure_artifact` carries only selected `content_sha256`; allowed-tuple matching is ambiguous.

## 4. Missing requirements

Strict TOML keys/required fields and slug grammar; snapshot DB constraints; canonical pin-map schema, history retention, atomic deployment, and malformed-map errors; RPC deadlines/cancellation; `manifest_invalid`; HTTP status contracts; and deploy/startup checks for existing custom-slug collisions.

## 5. Architecture/design notes

The gateway resolves recipes, selects site-specific pins, and never executes or transfers scripts. The runner validates the deploy map, paths, hashes, executes under its installer identity, uses per-`install_as` flock, and writes the sidecar. Locks are site-local; every site ensures independently. Airgap works only when the verified artifact already satisfies the pin and sidecar.

## 6. Top 5 concrete edits

1. Define Release A/B authority; require quiescing, atomic signed-map backfill, versioned export, and rollback.
2. Specify pin-map generation/deployment and make runner hash and execute the same immutable script bytes.
3. Define Connect site selection, install/OAuth ordering, timeout UX, and partial-row cleanup.
4. Add strict recipe validation and complete pin-map, manifest, artifact, and RPC error taxonomies.
5. Fix sidecar recovery to avoid network access, and enforce catalog/custom slug collision checks.
