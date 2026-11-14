**1. Verdict:** GO WITH FIXES — the split is sound, but the installer trust boundary, per-kind snapshot shape, and Connect-time blocking UX have holes that must be closed in the spec before code.

**2. Critical risks**
- **Snapshot vs pin map = two derivations, one never checked.** §8.1 says the runner validates `script_relpath`/`script_sha256`/`install_as` against its local pin map, but *never* validates RPC `content_sha256`. Meanwhile §6 makes the script authoritative for the URL ("URL honesty lives inside reviewed scripts"). So `content_by_arch` selection (§8.2) is advisory post-verify, not a pin. Say which is authoritative or a gateway bug silently installs whatever the script fetches and the hash check "passes."
- **Flat pin + multi-arch fleet undefined.** A `content_sha256` recipe shipped to an arm64 runner has no stated gate; the script's own arch logic decides. Add: flat pin ⇒ exactly one declared arch, else startup fail.
- **Atomic rename cross-device.** §8.3.4: scratch `cwd` + `atomic rename` into `/var/lib/portunus/bin` — EXDEV if scratch is under `TMPDIR`. Pin scratch to the same filesystem as `install_as`.
- **Migration: no pre-cutover artifact check.** Backfilled `artifact_snapshot` pins may not match binaries already on runners → mass reinstall / airgap hard-fail on first spawn after Release B.
- **§9 Release A** serves SQL "if needed" *and* TOML with no stated precedence; `PUT` writes become invisible on next GET.

**3. Contradictions / stale**
- §4 mixes live (`env_defaults`, manifest) and frozen (`argv`, paths) on one server row — an env edit can point at a path the frozen argv doesn't. Undocumented footgun.
- §4/§6 treat `artifact_snapshot` as uniform `id, kind, script, script_sha256, install_as, content_sha256/content_by_arch`; tree rows have no hashes and no script. Snapshot must be per-kind.
- §1's complaint ("seeds fight operator edits") is re-created as reserved-slug collisions in §7.

**4. Missing**
- Connect UX: which site is chosen, total budget vs 120s/30s, and how lock-timeout differs from real `spawn_failed` in the UI.
- Pin-map format/versioning + deploy atomicity (new map, stale scripts); per-site cache TTL/invalidation.
- Tree `install_as` ownership: deploy rsync writes as root, installer can't repair/mode-verify.
- Install audit log (server_id, artifact, requester, result).
- SPA int-`id` → string-slug break for existing API consumers; 410 body contract.

**5. Notes**
Runner executes; gateway orchestrates — correct. flock-under-installer-dir and sidecar-missing ⇒ fail-closed are good. Keep.

**6. Top 5 edits**
1. Add RPC↔pin-map equality check for `content_sha256`, or declare scripts sole authority and delete gateway-side pins.
2. Split `artifact_snapshot` schema by kind; specify tree rows exactly.
3. Require scratch on `install_as`'s filesystem; specify rename(2) semantics.
4. Add Release-A→B pre-flight: verify on-disk artifact hashes == new pins, per site.
5. Define Connect: site selection, overall deadline, distinct `install_pending`/`install_lock_timeout` codes.
