# Portunus TOML app catalog design — multi-model synthesis

Date: 2026-11-13  
Models (via `pi`): janus/deepseek-v4.1-flash, janus/glm-5.3-flash, janus/mimo-v2.6-flash, janus/MiniMax-M3.1-Flash, janus/qwen3.8-flash, janus/step-3.7-flash  
Raw replies: docs/audit/*.txt

## Verdicts

| Model | Verdict |
|---|---|
| deepseek-v41-flash | GO WITH FIXES — slug link + installer trust unspecified |
| glm-53-flash | GO WITH FIXES — drift, installer trust, tree contradiction |
| mimo-v26-flash | GO WITH FIXES — installer trust + tree underspecified |
| minimax-m31-flash | GO WITH FIXES — runner trust, versioning, migration |
| qwen38-flash | GO WITH FIXES — drift, installer, install lifecycle undecided |
| step-37-flash | GO WITH FIXES — runner trust, migration, CRUD removal |

**Overall: GO WITH FIXES (6/6).** No NO-GO. Spec must tighten before implementation.

## Consensus (≥5 of 6)

1. **Recipe↔server binding and drift (6/6).** Spec must state how a live server finds its recipe and what happens when TOML changes. Ground truth: `servers.slug` already exists and Connect already sets it — document that; add explicit drift policy (recommend: spawn-time skill-bridge inject/auth read **current** AppCatalog by `server.slug`; argv/env/url on the server row remain the Connect-time snapshot unless the operator reconnects).
2. **Installer trust boundary (6/6).** Runner executes install scripts; gateway must not assume a git checkout on the runner. Require script integrity (`script_sha256` in TOML), `install_as` under allowlisted prefixes, mandatory success hash, stderr redaction, lock + timeout.
3. **`tree` contradiction (5+/6).** Drop runtime “rsync-like copy” language. v1: trees are **deploy-guaranteed**; runtime step is verify-or-fail.
4. **Single decisions where the draft hedged (5+/6).** One startup policy (refuse to start); one SPA key (`id` = slug); one install trigger (before spawn; Connect may wait on the same path).
5. **Migration caution (5+/6).** Do not silent-wipe without stating that operator catalog CRUD disappears; export optional; table drop can be follow-up. Existing servers keep working via `slug`.

## Split opinions (do not auto-apply)

- **Pins in TOML vs script-internal pins:** qwen wants arch→{url,sha256} in TOML; others accept script-owned pins. **Keep for now:** script-owned pins + required `script_sha256` on the artifact; optional `[artifacts.pins]` as follow-up.
- **Async install job + status API:** qwen wants async; others OK with synchronous spawn wait ≤120s. **Keep for now:** synchronous before spawn; async status is deferred (§13).
- **Gateway ships script bytes over control plane:** glm suggestion. **Keep for now:** scripts/trees present on runner volume/image (same as today’s skill sync); no new control-plane blob channel in v1.

## Applied to doc

See `docs/superpowers/specs/2026-11-13-toml-app-catalog-design.md` updates from this audit.
