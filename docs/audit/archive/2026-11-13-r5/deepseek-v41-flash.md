## 1. Verdict
**GO WITH FIXES** — the split (recipes in TOML, live state in DB, installers executed on runners under a deploy-published pin map) is right, but the spec contradicts itself on how binding happens, leaves `install_as` aliasing undefined, and its strongest trust claim (“deploy-guaranteed trees”) verifies nothing.

## 2. Critical risks
- **catalog↔server split:** §4 says `servers.slug` is the binding, then declares `recipe_slug` is the binding. §9 step 3(a) classifies candidates by `slug` string equality — exactly what §4 forbids (“never bind a custom server solely because its slug string equals a recipe stem”). The migration rule is self-contradicting and will either mis-bind customs or abort.
- **Installer security:** the runner pin map is a *second* source of truth derived from TOML at deploy time. No requirement that gateway snapshot and runner pin map agree; drift fails the whole fleet at `artifact_hash_mismatch` with no fail-fast. A compromised gateway is contained, but a stale deploy silently bricks Connect.
- **`install_as` aliasing (highest severity, untested):** nothing enforces 1:1 between artifact and `install_as`. Two servers of one recipe with different `artifact_snapshot` pins (post-refresh) sharing one path, plus the flock keyed on `install_as`, produce install→overwrite→reinstall flapping on every spawn.
- **Tree is unverified:** existence-only, no hash allowed for `kind = "tree"` (§6 forbids `content_*` for trees). A partial rsync passes the check; anything writable by the installer identity becomes code injected into MCP children.
- **Multi-arch:** `content_by_arch` selects fine, but exit-78 mapping and arch re-advertisement after host/image replacement are unspecified.
- **Connect UX:** 120s synchronous install in a request, ≤30s lock wait, readiness UI deferred, and site selection at Connect unspecified while spawn may land elsewhere and re-install.

## 3. Contradictions / stale claims
- §4 vs §9 on slug-equality binding (above).
- §6: script “fetches its own pinned URL+SHA256” while TOML supplies `content_sha256` — two authoritative hashes; disagreement surfaces as a 120s `artifact_install_failed`, not a validation error.
- §8.1 “or ensure handshake” advertises arch — two arch-discovery paths.
- Frozen `oauth` fields + frozen vault secrets ⇒ reconnect-only token refresh, never stated.

## 4. Missing requirements
`manifest_invalid` (YAML is the only inject-time-mutable file), `pinmap_missing`/`pinmap_drift`, `installer_identity_unavailable`, `install_as_in_use`; redaction of `stderr_tail` (may contain signed URLs); atomic tree staging; no CI gate for `content_*` pins (“or equivalent CI” is not a contract).

## 5. Notes
Runner runs the script — correct. Lock should key on `(artifact_id, content_sha256)`, not `install_as`. Airgap story is honest but only works if content pins are refreshed on reconnect.

## 6. Top 5 edits
1. Rewrite §9 binding: require positive evidence (stored `catalog_app_id`/argv signature), explicit map for everything else.
2. Enforce unique `install_as` per artifact at startup; add the two-snapshot churn test to §12.
3. One authoritative content hash; specify gateway-vs-pin-map digest handshake with startup failure.
4. Either hash trees (file manifest) or downgrade §2.5/§8.3 to “existence only, tamper-undetected”.
5. Add the four missing error codes, `stderr_tail` redaction, and state Connect-needs-reconnect in the UI contract.
