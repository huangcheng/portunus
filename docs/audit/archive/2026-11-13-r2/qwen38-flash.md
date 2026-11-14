## 1. Verdict
**NO-GO** — the installer privilege model contradicts itself, and nothing verifies that the runner's copy of a script matches the gateway's `script_sha256`, so this ships a runner-wide supply-chain hole instead of closing one.

## 2. Critical risks
- **Privilege (§8.2):** install runs as the *MCP child uid*, but must create files under `/var/lib/portunus/bin|skills|skill-state/`. That makes every untrusted stdio server's uid able to write another app's binary → any MCP child can swap a binary before its next spawn. "Checksum-pinned" is void post-install (TOCTOU). Needs a dedicated installer uid; targets owned by it, read-only to MCP children.
- **Two distribution channels, no handshake:** `script_sha256` lives in gateway TOML; the bytes ship to runners (§8 Security). Platform downgrade or partial rollout = permanent `spawn_failed`. No version pairing defined.
- **Self-attested pins:** the script itself writes `install_as.sha256`, and the binary's expected hash lives only in the script body (`[artifacts.pins]` deferred). The "sidecar matches installed object" check is circular.
- **Migration (§9.3–4):** the claim that all `servers.slug` values match is unverified. Renaming/deleting one TOML silently turns a working app into **Custom** + `recipe_missing`. Before "delete all `catalog_apps` rows," prove vault/grant/tool keys derive from slug, not the numeric PK — otherwise secrets are unrecoverable.

## 3. Contradictions / stale claims
- Lock wait 30s vs script timeout 120s: "waiters re-check after lock" can never happen; the 2nd concurrent spawn always fails.
- "detect arch itself" vs "informational `PORTUNUS_ARCH`" — pick one; scripts trusting the env is a footgun.
- `flock` + "stale lock → error": flock releases on death; "stale" only exists in pidfile schemes.
- §3 forbids gateway→runner blob sync, yet §8 needs "the recipe payload on the runner" — an undescribed RPC.
- §4: inject reads live `env_defaults` while secrets stay a Connect snapshot → renaming a spec key breaks live servers with no audit trail.
- §12 lists unit-style tests; AGENTS.md mandates E2E only.

## 4. Missing
Gateway↔runner artifact-ensure RPC (auth, idempotency, timeout budget vs HTTP Connect wait); supported-arch list for Connect pre-check; retry/backoff + negative cache for doomed installs; redaction spec (patterns, length); tree digest mandatory-or-optional; collapse `artifact_install_failed`/`spawn_failed` to one code; CI TOML lint (one typo bricks the gateway).

## 5. Architecture notes
Airgap "binary present, sidecar matching" mislabels image-baked binaries as missing. Define a *pre-provisioned* state.

## 6. Top 5 edits
1. Installer uid ≠ MCP child uid; state target dirs non-writable to children.
2. Runner-side script digest manifest + gateway version handshake.
3. Restore `sha256` pins in TOML, verified out-of-band by runner.
4. Lock wait ≥ script timeout, or make ensure async with `pending`.
5. Startup orphan-slug audit; explicit §9 precondition that nothing keys off `catalog_apps.id`.
