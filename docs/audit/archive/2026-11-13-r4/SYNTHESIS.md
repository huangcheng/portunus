# Portunus TOML app-catalog design audit — multi-model synthesis

Date: 2026-11-13 (round 4)
Models (via `pi`): janus/deepseek-v4.1-flash, janus/glm-5.3-flash, janus/mimo-v2.6-flash, janus/MiniMax-M3.1-Flash, janus/qwen3.8-flash, janus/step-3.7-flash
Raw replies: docs/audit/*.txt (previous: archive/2026-11-13-r1 … r3)

## Verdicts

| Model | Verdict |
|---|---|
| deepseek-v41-flash | GO WITH FIXES — runner install_as check, per-arch pins, backfill |
| glm-53-flash | GO WITH FIXES — backfill, multi-arch, runner path validation |
| mimo-v26-flash | GO WITH FIXES — install_as dual SoT, arch-hash, blind backfill |
| minimax-m31-flash | **NO-GO** — installer trust, multi-arch, migration, manifest delivery |
| qwen38-flash | GO WITH FIXES — lock vs timeout, runner path, content_by_arch shape |
| step-37-flash | GO WITH FIXES — runner allowlist, migration collisions |

**Overall: GO WITH FIXES plurality (5) + 1 NO-GO.** Must clear multi-arch pin model, runner-side `install_as` enforcement, and migration provenance before calling the design audit-passed.

## Consensus (≥5 of 6)

1. **Runner must independently enforce `install_as` prefix / path normalization (5+).** Gateway-only TOML checks are insufficient if the control plane is compromised.
2. **Exactly one of flat `content_sha256` XOR `content_by_arch`; runner must advertise GOOS/GOARCH (5+).** Single hash cannot validate multi-arch outputs; optional table without handshake is incomplete.
3. **Blind `recipe_slug = slug` backfill is unsafe (5).** Need pre-scan, match TOML slug values (not stems only), and do not auto-bind true customs / operator-only recipes.

## Near-consensus (apply)

1. **`install_as` dual source of truth (3–4):** freeze artifact descriptors at Connect for ensure+argv, or make spawn paths live from recipe — pick one.
2. **`PORTUNUS_APPS_DIR` default vs “missing → exit” (3):** exit only when explicitly set and bad; packaged default otherwise.
3. **Runner artifact allowlist (2–3, drives NO-GO):** deploy a local pin map so gateway cannot point at arbitrary deployed scripts — apply a minimal version (artifact_id → script_relpath from same deploy package).

## Split opinions

- Full signed RPC / immutable sidecar metadata → defer (step/minimax).
- `recipe_rev` in v1 → defer (qwen); keep live manifest/env.
- Lock wait ≥ 120s → keep 30s flock wait; document concurrent-spawn failure under long install (glm/qwen).
- Collapse `slug`/`recipe_slug` → keep both; document divergence (custom slug display vs bound recipe).

## Applied to doc

Round-4 consensus folded into `docs/superpowers/specs/2026-11-13-toml-app-catalog-design.md`.
