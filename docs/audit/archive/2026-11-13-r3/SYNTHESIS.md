# Portunus TOML app-catalog design audit — multi-model synthesis

Date: 2026-11-13 (round 3)
Models (via `pi`): janus/deepseek-v4.1-flash, janus/glm-5.3-flash, janus/mimo-v2.6-flash, janus/MiniMax-M3.1-Flash, janus/qwen3.8-flash, janus/step-3.7-flash
Raw replies: docs/audit/*.txt (previous: archive/2026-11-13-r1, archive/2026-11-13-r2)

## Verdicts

| Model | Verdict |
|---|---|
| deepseek-v41-flash | GO WITH FIXES — slug binding + self-referential install hash unsound |
| glm-53-flash | GO WITH FIXES — no-op hash source missing; Orphaned/artifact scope undefined |
| mimo-v26-flash | GO WITH FIXES — tree digest / lock budget / RPC trust underspecified |
| minimax-m31-flash | GO WITH FIXES — recipe identity/drift + installer trust must be explicit |
| qwen38-flash | GO WITH FIXES — slug privilege hole; install-hash oracle; per-site install |
| step-37-flash | GO WITH FIXES — runner provenance + artifact contracts under-specified |

**Overall: GO WITH FIXES (6/6; 0 NO-GO).** Round 2 privilege NO-GO cleared; residual is install-hash oracle, tree digest, binding/orphan detection, and runner RPC trust wording.

## Consensus (≥5 of 6 = ⌈N×0.7⌉)

1. **Install no-op needs an out-of-band content hash (5+).** `script_sha256` authenticates the script only; “expected content hash” for `install_as` has no TOML/RPC source. Add `content_sha256` (or per-arch equivalent) on `[[artifacts]]` + RPC; sidecar must match that pin, not only self-hash.
2. **`tree_digest` is referenced but undefined (5).** Either add it to the schema with an algorithm, or drop digest language and keep existence-only verify for `kind=tree`.
3. **Runner↔gateway trust must be stated (5).** Hash-in-RPC alone is weak without naming the control-channel auth and that deploy disk is the trust root (runner refuses missing/mismatched local bytes).

## Near-consensus / objective contradictions (apply anyway)

1. **`flock` “stale → error” is wrong (3–4).** Kernel releases flock on death; rewrite wait/timeout wording.
2. **Slug-as-sole binding / Orphaned undetectable (4).** Client can `POST /servers` with a shipped slug; removed slugs cannot be distinguished from Custom. Need Connect-time provenance (`recipe_slug` or `catalog_bound`) and reject/ignore client slug unless in catalog.
3. **Live `env_defaults`/manifest vs frozen argv (4).** Document compatibility: freeze non-secret env key set at Connect **or** accept live inject with explicit restart semantics; clarify manifest read-at-inject vs startup cache.
4. **SPA `id` int→slug + 410 ordering (4).** Ship SPA that stops mutating `/api/apps` before/with 410; note rolling-deploy risk.

## Split opinions (do not auto-apply)

- **Per-arch `content_sha256` matrix in TOML vs pins only inside scripts:** deepseek/minimax want TOML pins; prior product choice deferred `[artifacts.pins]` URL table. → **Decision owner = user** if single `content_sha256` is too weak for multi-arch fleets.
- **Freeze all env at Connect vs live TOML env_defaults:** deepseek/qwen prefer freeze/version; current §4 says live. → keep live for v1 unless user flips.
- **Runner-local pin manifest / signed RPC:** step/deepseek want independent runner pins; v1 keeps deploy-sync + RPC hash match. → defer signatures.
- **Detach-to-Custom endpoint for orphans:** deepseek only. → defer.
- **Lock wait ≥ N×120s vs 30s:** mimo. → keep 30s flock wait; document overall ensure budget separately if needed.

## Applied to doc

See `docs/superpowers/specs/2026-11-13-toml-app-catalog-design.md` (round 3 consensus edits): `content_sha256`, tree existence-only, RPC auth wording, flock timeout, `recipe_slug`/`catalog_bound`, Connect slug rules, SPA/410 note, manifest inject-time read.
