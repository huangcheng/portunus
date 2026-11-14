## 1. Verdict
**GO WITH FIXES** — the storage split is sound, but the spec has one privilege hole (`slug` trust), an undefined install-hash oracle, and no per-runner-site install model despite shipping a multi-runner fleet.

## 2. Critical risks
- **`slug` is a binding and an auth-like selector, yet unvalidated.** §7 lets Connect "copy recipe → `POST /servers` with `slug`". Nothing says a client cannot create a *custom* server with `slug = "dropbox"` — it then inherits that recipe's live `env_defaults`, manifest, and artifacts. Same for `PATCH` slug rebind. Recipe content becomes attacker-selectable.
- **Live-from-TOML `env_defaults` + manifest (§4)** mutate running servers with no code deploy and no version pin. Renaming a spec key silently breaks vault lookups at inject; swapping `manifest` points a vaulted secret at a different CLI. "No DB migration" ≠ "safe to hot-edit", but the spec implies both.
- **Install pin is not out-of-band.** `script_sha256` comes from the same TOML as the script it hashes; one commit satisfies both sides. The only control is review (ocr gate) — a code-exec channel to every runner that needs an independently published allowlist or signature.
- **`tree_digest` is unspecified cross-host**: the gateway has `third_party/`, the runner has `/var/lib/portunus/skills/`; who computes/compares is never stated.
- **Fleet skew**: no capability/version check for `ensure_artifact` against older runners.

## 3. Contradictions / stale claims
- §3 defers "operator TOML overlays"; §6 makes `PORTUNUS_APPS_DIR` operator-configurable — overlay by another name.
- §8.1 "not a new blob RPC" while §8.3 introduces a new gateway→runner RPC.
- §8.3 step 3: "if `install_as` + sidecar already match **expected content hash**" — no expected hash exists anywhere (§5 excludes `[artifacts.pins]`). Runner can only re-verify sidecar self-consistency, so the airgap "no network" claim is unproven.
- `flock` "stale → error" is meaningless (process death releases it) unless NFS-locked; say so.
- "before first spawn" vs Connect "same ensure path" — which *site* does Connect install to, if spawn lands elsewhere?

## 4. Missing requirements
Reserved/immutable `slug`; total install deadline (30s lock + 120s script × N artifacts ≫ HTTP timeout); `PATH`/proxy/`TMPDIR`/CA in the "env allowlist only"; atomic **rename**-into-place (ETXTBSY on overwrite of a running binary); install audit log; deploy-time derivation of required `tree` dirs from TOML; disk quota/scratch cleanup; artifact support for custom servers (currently impossible — state it).

## 5. Notes
Runner executes (correct: gateway is remote to disk), so gateway ships *intent*, runner owns *trust*. Lock on `install_as.lock`, not per-artifact-id.

## 6. Top 5 edits
1. Server-create: reject/ignore client-supplied `slug` unless in `AppCatalog`; freeze slug post-connect.
2. Add `recipe_version` snapshot column — pin manifest/`env_defaults` at Connect, hot-apply opt-in.
3. Bind `ensure_artifact` to the spawn-target site + fleet capability check + overall deadline/retry.
4. Define the install-as oracle: script-declared `content_sha256`, rename-into-place, separate reviewed installer allowlist.
5. Ship SPA before `410 Gone`; add `schema_version` to TOML.
