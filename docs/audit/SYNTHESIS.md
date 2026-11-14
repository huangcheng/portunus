# Portunus TOML app-catalog design audit — multi-model synthesis

Date: 2026-11-13 (round 6 — audit-passed)
Models (via `pi`): janus/deepseek-v4.1-flash, janus/glm-5.3-flash, janus/mimo-v2.6-flash, janus/MiniMax-M3.1-Flash, janus/qwen3.8-flash, janus/step-3.7-flash
Raw replies: docs/audit/*.txt (archive: r1–r5)

## Verdicts

| Model | Verdict |
|---|---|
| deepseek-v41-flash | GO WITH FIXES — conflict remedy / TOCTOU / egress knob |
| glm-53-flash | GO WITH FIXES — airgap/sidecar / kind-conditional checks / Release A |
| mimo-v26-flash | GO WITH FIXES — lock wait / env_defaults / migration digest |
| minimax-m31-flash | GO WITH FIXES — enforcement / migration atomicity / runtime contracts |
| qwen38-flash | GO WITH FIXES — pin generation / snapshot validation / A→B hole |
| step-37-flash | GO WITH FIXES — supply-chain / pin-history / error completeness |

**Overall: GO WITH FIXES (6/6), 0 NO-GO** (also 0 NO-GO on round 5). Architecture consensus holds; residual items are sizing/UX/signing splits or one-line clarifications.

## Consensus applied this round (≥ near-threshold)

1. Sidecar missing + content hash OK → write sidecar, do not reinstall.
2. Startup hard-fail on TOML slug vs custom `servers.slug` collision.
3. `recipe_slug` null ⇔ empty snapshot; else `artifact_snapshot_missing`.
4. `allow_install_egress` runner knob; fd-hash/`fexecve` (no path TOCTOU).
5. `migration_map.apps_digest`; pin-map history retention window; CI-generated pin map.
6. Clarify `PORTUNUS_APPS_DIR` replace vs deferred overlay merge.

## Split opinions (deferred — owner = product)

- Lock wait ≥ 120s vs queue-behind-ensure vs async install API.
- Freeze `env_defaults` into snapshot vs keep live inject.
- Signed pin maps / signed RPCs in v1.
- Tree content hashing in v1.
- Connect readiness UI / site-selection UX polish.

## Applied to doc

`docs/superpowers/specs/2026-11-13-toml-app-catalog-design.md` marked **audit-passed**. Ready for an implementation plan (separate doc); OCR gated-commit for this design + audit artifacts.
