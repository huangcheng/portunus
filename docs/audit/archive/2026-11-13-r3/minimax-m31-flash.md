## 1. Verdict

**GO WITH FIXES** — the architecture is viable, but recipe identity/drift and installer trust must be explicit before implementation.

## 2. Critical risks

- **Catalog↔server split:** Live `artifacts`, manifest, and `env_defaults` can diverge from frozen command/argv/OAuth. A changed artifact path can break an existing server; changed env keys can miss its vault. Current `AppCatalog` also cannot tell a removed shipped slug from a custom slug, and a custom server may collide with a shipped slug.
- **Installer security:** A gateway-supplied hash is not a runner trust anchor if deployed files are mutable. Hash-then-open/exec permits TOCTOU/symlink attacks; running as the runner daemon’s uid also lets a compromised daemon forge artifacts. Sidecar ownership, script provenance, and output verification are unclear.
- **Multi-arch:** `script_sha256` authenticates the script, not its output. No per-architecture desired binary digest exists, so “expected content hash” and airgap no-op are undefined. A script can also block for 120 seconds despite “fail fast.”
- **Migration:** Removing CRUD can lose operator-created recipes. No mandatory conversion, deploy ordering, rollback, or compatibility behavior for `catalog_apps` readers is specified.
- **Skill-bridge/Connect:** Manifest or environment drift may be incompatible with a frozen server. Client-side recipe copying permits listing/Connect races and recipe removal between checks. Numeric app-ID assumptions remain a SPA migration risk.

## 3. Contradictions or stale claims

- “Expected content hash” has no TOML source; `script_sha256` covers only the script.
- `tree_digest` is optional in the RPC but absent from the recipe example, with no algorithm.
- “Orphaned recipe” cannot be derived from the current in-memory catalog alone.
- Errors vary between `spawn_failed`, `artifact_install_failed`, and “or `spawn_failed`”; HTTP/MCP mappings are undefined.
- Live artifacts conflict with frozen argv unless artifact versions are coordinated.

## 4. Missing requirements

Define strict schema/versioning, slug/artifact-ID uniqueness and `install_as` collision rules; manifest path/existence checks; canonical path handling; runner-authenticated RPC; a runner-side trusted deployment hash manifest; independent installer identity; process-group timeout; env/network policy; stable error payloads; tree digest algorithm; and reserved-slug/custom-origin rules.

## 5. Architecture/design notes

The runner should execute, not the gateway: the gateway sends recipe revision plus artifact metadata; the runner locks, opens/verifies one file descriptor—or an immutable copy—and executes in an isolated worker. Use local `flock`, then re-verify. Airgap should compare a declared desired digest and avoid network entirely.

## 6. Top 5 concrete edits

1. Add `recipe_revision`, server recipe provenance, and coordinated artifact/env compatibility rules.
2. Add per-arch output digests, required tree digests, and a versioned sidecar format.
3. Specify runner-side script pinning, symlink-safe same-file execution, installer isolation, and timeout/network controls.
4. Make Connect resolve recipes server-side; reserve shipped slugs and retain known shipped-slug history.
5. Add staged migration/rollback and exact API/MCP error contracts, including deterministic install tests.
