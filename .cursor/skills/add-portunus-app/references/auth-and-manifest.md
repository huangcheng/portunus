# Auth and manifest reference

Source of truth: `packages/portunus_skill_bridge/portunus_skill_bridge/manifest.py` (`AuthSpec`, `Manifest`).

## Auth types

| `auth.type` | Connect UX | Typical backend |
|---|---|---|
| `bearer_env` / `header_env` | Secret field(s) in Connect; vaulted env | `http_rpc` |
| `device_qr` | Show CLI QR PNG; poll status | `json_cli` |
| `oauth_link` | Open authorize URL (popup/tab); optional paste code; optional browser_capture job | `json_cli` |
| `none` | No Connect auth | rare |

`external_session` is a deprecated alias of `device_qr`.

Platform auth (`device_qr` / `oauth_link`) requires `backend: json_cli`.

## Shared AuthSpec fields

- `login_env` — injected **only** for Connect `skill_exec` auth (e.g. `GUANGYAPAN_DRIVE_LOGIN_MODE: device`). Not for normal tool calls. Do not use it to impersonate another agent runtime.
- `actions` — CLI subcommands: `login`, `status`, `logout` (defaults: `login`, `login-status`, `logout`).
- `connect_hint` — optional operator copy under Connect (≤500 chars).
- `poll_interval_s` — Connect poll cadence (default 2).

## device_qr

- `qr_path_field` — key under `result.data` for PNG path (default `qr_code_path`).
- CLI login must write a QR image the runner can base64; Connect polls until logged in.
- Example: `manifests/guangyapan.example.yaml`.

## oauth_link

- CLI login returns an https authorize URL (often in `msg` / result); runner/gateway surface `auth_url`.
- `token_flag` — paste-code flag (default `--token`).
- `success_code` / `pending_codes` — numeric CLI result codes (Quark uses `0` success, `-114`/`-106` pending).
- `browser_capture: true` — headless runners: shim `xdg-open`, keep CLI alive, return `job_id`; Connect polls `status` with sticky site routing.
- `capture_timeout_s` — how long the runner keeps the login process (default 90).
- `token_paste` — show paste-code fallback in UI (default true).
- **Never** render authorize URLs as QR for operators. Popup/tab + copy only.
- Example: `manifests/quark.example.yaml`.

## bearer_env / header_env

- `env` — vault secret name (must appear in catalog `spec.secrets`).
- `header` / `prefix` — HTTP auth header construction.
- Example: `manifests/weread.example.yaml`.

## json_cli

```yaml
cli:
  command: ["node", "scripts/app.cjs"]  # or bin/...
  cwd: ${SKILL_ROOT}
  identity:
    agent_name: Portunus          # omit entire identity: {} if CLI rejects flags
    agent_profile_dir: ${STATE_DIR}
  run_wrapper: raw                # or run_operation
```

- `run_wrapper: raw` — each tool maps to a CLI subcommand via `tools[].cli`.
- `run_wrapper: run_operation` — tools use `tools[].operation` inside a `run` wrapper.
- Expand `${SKILL_ROOT}` / `${STATE_DIR}` from child env (`spec.env_defaults`).

## http_rpc

```yaml
http:
  base_url: https://api.example.com/...
  method: POST
  dispatch_field: api_name
  const: { skill_version: "1.0.0" }
  timeout_s: 60
```

Each tool needs `dispatch` matching the vendor API name.

## Catalog `spec` for skill_bridge

```yaml
# conceptual — Python dict in catalog.py
spec:
  manifest: <loaded example.yaml as dict>
  secrets: [{ name: WEREAD_API_KEY, label: "...", secret: true }]  # or []
  env_defaults:
    SKILL_ROOT: /var/lib/portunus/skills/<dir>
    STATE_DIR: /var/lib/portunus/skill-state/<slug>
```

`validate_spec` requires bearer/header `auth.env` ∈ `secrets`.
