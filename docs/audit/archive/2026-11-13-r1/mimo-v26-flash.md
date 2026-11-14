**1. Verdict:** GO WITH FIXES — the ownership split is sound, but the installer trust boundary and the tree-artifact story are underspecified enough to cause runner-side ambiguity at implementation time.

**2. Critical risks**
- **Catalog↔server split:** synthetic `id = slug` vs UUID — spec offers "or switch SPA" without deciding; JOINs and stale SPA caches of old UUIDs during migration are unaddressed.
- **Installer security:** allowlist dir is good, but no verification that the *runner* enforces it — scripts execute with runner privileges; spec never states what user/sandbox they run under, nor whether env `PORTUNUS_INSTALL_AS` can be poisoned by a malicious recipe (it's repo-controlled, but the check isn't stated).
- **Multi-arch:** scripts must "detect arch" but no canonical `PORTUNUS_ARCH` values (amd64/arm64?) nor failure behavior on unsupported arch — timeout at 120s is the only bound.
- **Migration off `catalog_apps`:** step 3 says "delete rows" and "optionally drop table in follow-up" while §10 removes CRUD in v1 — if CRUD is gone in the same release, defer-drop is unnecessary; ordering (API removal before row deletion?) isn't sequenced against running gateways.
- **Skill-bridge inject:** resolution by slug is fine, but no fallback for existing `servers.slug` values that don't match any TOML stem (renamed/removed app) — §11 only covers "unknown slug for skill_auth."
- **Connect UX:** §8 allows triggering install "before first spawn or at Connect" — two trigger points with different error surfaces; Connect-time failure semantics vs spawn-time (`spawn_failed`) are conflated.

**3. Contradictions / stale claims**
- §6 says invalid file → "refuse to start (or skip with hard error in prod — refuse to start)": parenthetical is self-contradictory; pick one.
- §7 says `POST/PATCH/DELETE` → "404 or gone" — contract should be one status code.
- §9.5 "remove Catalog Config mutations" vs §3 non-goal implies CRUD already gone — sequencing unclear.
- §8.2 admits trees are deploy-time, yet §2 goal 4 lists artifacts as on-demand — `tree` kind is effectively dead code in v1; say so.

**4. Missing requirements**
- Runner trust boundary: which UID, network egress policy, and whether installers can be run on a compromised runner without gateway exposure.
- Explicit `tree` vs `install_script` decision for v1 (see 3).
- SPA migration path: old UUID-keyed bookmarks/API consumers.
- Checksum enforcement for `tree` artifacts (only install_script has SHA256).

**5. Architecture notes**
- Gateway should *not* execute scripts; runner executes under per-server lock — spec hedges ("gateway or runner helper"); make runner-only so gateway never has egress or shell. Locking on `install_as.lock` is correct; add lock scope across servers sharing one artifact. Airgap: only pinned-URL scripts work; declare offline bundles out of scope explicitly.

**6. Top 5 edits**
1. Mandate runner-side execution with a named service user and egress allowlist in §8.
2. Resolve `id = slug` vs UUID with a single normative choice + SPA migration note (§7, §10).
3. Fix §6 parenthetical → single startup-failure rule; fix §7 status code.
4. Declare `tree` artifacts deploy-time-only (remove from on-demand lifecycle) or specify hash sync.
5. Add error row for "existing server slug not in catalog" and unsupported-arch failure (§11).
