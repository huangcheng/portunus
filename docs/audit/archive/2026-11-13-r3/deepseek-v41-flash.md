## 1. Verdict
**GO WITH FIXES** — the TOML/live-data split is right, but slug-as-sole-binding and self-referential hash verification are unsound as written.

## 2. Critical risks
- **No recipe provenance column.** Any `POST /servers` with `slug="quark"` inherits live manifest + `env_defaults` and can trigger installer-uid script execution. §4/§9.3's "no new column required" is the bug: add `servers.recipe_slug`, set only by Connect, NULL = custom.
- **`install_as.sha256` is written by the script itself**, so the no-op short-circuit compares script output to itself. Only a TOML-declared expected `install_as_sha256` verifies anything — `[artifacts.pins]` is *not* deferrable, it's the sole correctness check.
- **Frozen argv + live `env_defaults`/manifest** ⇒ child behavior isn't a function of the snapshot; a TOML edit silently changes running servers. Freeze env at Connect, or version recipes and require reconnect.
- **Deploy ordering.** Gateway `script_sha256` and runner script bytes ship separately; a one-sided rollout fails every spawn for that recipe fleet-wide. Content-address filenames (`install-rclone.<sha8>.sh`), accept N-1.
- **Runner trust boundary underspecified.** Hash-in-RPC only proves gateway/disk agreement. Runner should hold its own deploy-pinned manifest and refuse anything outside it. Also: which gateway may call `ensure_artifact`? No authn stated.
- **Multi-arch.** Script self-detects arch; nothing verifies the *binary* is the expected arch → wrong-arch install "succeeds".

## 3. Contradictions / stale claims
- "Airgap: fail fast, no long hang" vs the 120s timeout — no detection mechanism; DNS hang still eats 120s.
- `flock` never goes stale (released on death); "stale lock → error" describes lockfiles, not flock.
- Env allowlist of two vars breaks the scripts it exists to run (`PATH`, `HOME`, `TMPDIR`, `SSL_CERT_FILE` needed by curl/tar).
- `tree` "optional digest" contradicts verify-only guarantees.
- `label` **and** `name` both present, roles undefined.
- "No FK needed" is asserted, not verified — yet `id` flips integer→string slug, a breaking API/SPA change with no versioning and no same-release removal of Catalog Config UI.

## 4. Missing requirements
- Error codes: missing vault secret for a newly-declared recipe secret; manifest file not found; `ensure_artifact` transport failure/retry; empty `PORTUNUS_APPS_DIR` at startup; duplicate `install_as` across recipes (collision undefined).
- **Orphaned recipe is a dead end** — no detach-to-Custom endpoint; server errors forever.
- No per-site artifact state (server rescheduled to another runner must re-ensure).
- Connect UX: N×120s synchronous block, no progress state, no retry, and no answer to *which site*.
- No cleanup contract for partial installs / scratch dir.

## 5. Design notes
Gateway issues intent + hashes; the runner owns execution and policy (its own pin manifest, egress allowlist, airgap flag). Lock per `install_as` content hash, inside the installer-writable dir. Airgap must be a config flag, not timeout discovery.

## 6. Top 5 edits
1. Add `servers.recipe_slug` as the binding; delete the string-membership rule.
2. Declare per-arch `install_as_sha256` in TOML and enforce it; un-defer pins.
3. Content-address installer scripts, pin them runner-side, specify deploy order.
4. Freeze env/secrets keys at Connect (or version recipes); add `recipe_missing` remediation + detach endpoint.
5. Fix airgap detection, script env allowlist, stale-lock wording; add the missing error contracts.
