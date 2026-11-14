## 1. Verdict

GO WITH FIXES — the TOML/packaging direction is right, but the spec never defines how a connected `servers` row learns its slug (the one thing every migration step depends on), and the installer section leaves the trust boundary, executor identity, and airgap path unspecified.

## 2. Critical risks

- **Catalog↔server split is undefined, not just risky.** §7/§9 say skill-bridge inject resolves "by slug via AppCatalog", and §10 joins on `server.slug`, but no migration adds that column and no backfill exists for servers created before it. Deleting `catalog_apps` (§9.3) removes the only current name→recipe link. This breaks Connect for existing installs.
- **Drift policy absent.** Server rows copy argv/url at Connect. When a TOML recipe changes (new flag, renamed manifest, new artifact), nothing says whether spawn re-derives from TOML or keeps the snapshot. Either answer must be written down; both have failure modes.
- **Installer is arbitrary code execution.** An allowlisted shell script run at spawn is a root-equivalent capability shipped as data. No signature check, no justification for why path allowlisting is sufficient. `$PORTUNUS_ARCH` from the gateway contradicts §6 "script MUST detect arch".
- **Migration is one-way and self-bricking.** Startup fail on a single bad TOML kills the gateway with no `--skip-catalog` fallback and no downgrade path.
- **Airgap breaks Connect.** `install_script` needs network at first spawn; §3's non-goal constrains URLs, not behavior. No "already installed → skip" or disable switch.

## 3. Contradictions

- §6: "refuse to start (or skip file with hard error in prod — **refuse to start**)" — three policies in one sentence.
- §6 requires checksum-pinned installs; §8.3 makes `.sha256` sidecar *optional*.
- §5 shows repo-relative `deploy/installers/...`; §8.3 says "shipped with the platform" — a runner has no git checkout.
- §7 synthetic `id` = slug vs §10 slug keying vs `server.slug`.
- §2 says no operator recipes, yet §8.3 allows a "configured allowlist dir" — that is an operator-supplied script channel.

## 4. Missing requirements

Error contracts: lock wait timeout (only script timeout given), checksum mismatch, unsupported arch, partial install (`<install_as>.tmp` cleanup), disk full, runner missing script, privilege denial. Runner trust boundary: uid, writable roots, network egress, volume persistence under `/var/lib`. SPA: `/api/apps` pagination/caching and 410-vs-404 for removed mutations plus a compat window.

## 5. Design notes

Installer runs on the **runner**, not the gateway, so artifacts must ship in the runner image/volume — state that explicitly and give the pre-seed contract. Lock per `install_as`, bounded, stale-lock reclaim. Arch detection authoritative in-script; env informational only.

## 6. Top 5 edits

1. Add `servers.recipe_slug` + backfill; declare TOML-wins-on-spawn drift policy.
2. Pin the executor: runner uid, writable roots, required SHA256 sidecar, atomic rename, lock timeout.
3. One key: slug everywhere; document `/api/apps` shape and 410 window.
4. Offline contract: hash-match short-circuit, disable switch, explicit unsupported-arch error.
5. Choose one startup policy; add the missing §11 rows.
