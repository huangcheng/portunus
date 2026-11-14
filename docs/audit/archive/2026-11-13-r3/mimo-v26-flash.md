**Verdict: GO WITH FIXES** — the recipe/DB split is sound, but the installer contract has an undefined input (tree digest), an unsatisfiable lock budget, and an unspecified runner↔gateway trust boundary.

**Critical risks**
- **Catalog↔server:** "live" `env_defaults` vs frozen `args` lets a TOML edit change runtime behavior without reconnect — drift line is arbitrary. §9.4's export-then-delete doesn't check which existing servers still need those `catalog_apps` rows before deletion.
- **Installer security:** the runner validates `script_sha256` against disk bytes, but the hash arrives in an RPC with no stated authentication — a compromised gateway authorizes arbitrary bytes.
- **Multi-arch:** "script detects arch" with no supported-arch matrix, no atomicity rule (a partial install must never leave a matching sidecar).
- **Migration:** `id` changes int→string; SPA + any external consumers break silently. 410 must ship atomically with a client that stops calling mutations.
- **Skill-bridge:** `tree_digest` appears in §8.1/8.3 but no TOML field produces it; §6 defines only `path` as "hash baseline." Existence-only verify makes §2.5's "verify at runtime" vacuous.

**Contradictions / stale claims**
- Lock: `flock` has no stale state (kernel releases on death); "stale → error" is undefined. Wait ≤30s vs script timeout 120s means legitimate concurrent waiters always fail.
- Airgap "fail fast" has no mechanism — offline detection is an unstated script duty.
- `skill-state/` is an allowed `install_as` prefix, but no artifact kind writes there.
- §9.4 (delete all rows) vs §11 has no case for servers still referencing a removed row.

**Missing requirements**
- RPC authentication; sidecar hash definition (file vs directory/tree); total ensure budget per spawn; runner-site-unreachable behavior; API versioning for the `id` change.

**Architecture notes**
- Install on the runner is right (gateway stays off the data path). State explicitly: v1 sends hash-only, not bytes; define behavior for "file missing on runner" (deploy failure, distinct from hash mismatch).

**Top 5 edits**
1. Add `tree_digest` to the schema, or delete all digest language and normatively specify existence-only verify.
2. Redefine the lock: pid/heartbeat-based staleness; wait budget ≥ (artifacts × 120s), not 30s.
3. Specify `ensure_artifact` RPC auth and split failure codes: runner unreachable vs hash mismatch vs missing script.
4. Add arch matrix, install atomicity + sidecar-write ordering, and an offline-detection contract for scripts.
5. Resolve the `env_defaults` liveness question, drop `skill-state/` from the prefix list, and add the `id`-change/410 client migration step.
