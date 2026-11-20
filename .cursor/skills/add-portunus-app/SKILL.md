Temporal.io---
name: add-portunus-app
description: >-
  Add a Portunus Apps catalog service (oauth, http, webdav, stdio, or
  skill-bridge) the same way Quark/Guangyapan/WeRead were wired: packaged
  manifest, DEFAULT_APPS seed, icons, runner skill tree, Connect auth, deploy
  sync, and probe. Use when the user asks to add an app, catalog entry, skill,
  drive, or MCP service to Portunus.
---

# Add a Portunus App

Ship a **catalog app** operators Connect in the dashboard. Prefer **seeded recipes** in `gateway/portunus_gateway/catalog.py` (`DEFAULT_APPS`) so deploys do not need a SPA rebuild to discover the app.

## Choose the connection

**Prefer remote API / MCP endpoints over CLIs** (see root `AGENTS.md`). Use
hosted Streamable HTTP / SSE / OAuth MCP when the vendor offers it; use local
`stdio` / `uvx` / CLI skill-bridge only when no usable remote exists.

| `connection` | When | Spec lives in |
|---|---|---|
| `oauth` | Remote MCP + OAuth (preferred when available) | `spec.url` or `url_from_operator` + authorize/token (+ scopes) |
| `http` | Remote MCP, bearer/cookie elsewhere or open | `spec.url` or `url_from_operator` |
| `webdav` | WebDAV via `webdav-mcp-server` | `root_url`, `root_path` |
| `stdio` | Local MCP argv — last resort | `command`, `args`, `env` secret defs |
| `skill_bridge` | Vendor CLI / HTTP RPC wrapped as MCP | packaged YAML + `spec.manifest` |

**Default for HTTP API gateways:** `skill_bridge` + `http_rpc` (WeRead) when there is no remote MCP. **Default for vendor drive/CLI skills with no remote MCP:** `skill_bridge` + `json_cli` (Quark, 光鸭, Proton Drive).

Copy nearest peer:

- OAuth remote → `ardot` / `dropbox` seeds
- WebDAV → `nutstore` / `koofr`
- API key HTTP → `packages/portunus_skill_bridge/manifests/weread.example.yaml`
- Device QR CLI → `guangyapan.example.yaml`
- Browser-login official CLI (URL in terminal, no paste code) → `proton-drive.example.yaml` (`oauth_link` + `browser_capture` + `token_paste: false`)
- Authorize-link + paste code CLI → `quark.example.yaml` (`oauth_link` + `browser_capture` + `token_paste: true`)
- Remote MCP with credential-in-URL → `xunlei` (`connection: http`, `url_is_credential: true`; classic SSE if path is `…/sse` or `…/sse/<token>`)

## Skill-bridge path (most common)

### 1. Vendor runtime (json_cli only)

1. Vendor under `third_party/<skill>/` (document in `third_party/README.md`).
2. Runner path convention:
   - `SKILL_ROOT=/var/lib/portunus/skills/<dir>`
   - `STATE_DIR=/var/lib/portunus/skill-state/<slug>`
3. Deploy the tree to **every** runner host (not only the gateway). Probe and Connect exec on the runner.
4. Register the tree in `deploy/fleet-skills.sh` (`state_slug_for` / `entrypoint_for`) when the dir name ≠ slug or the entrypoint is non-obvious.
5. Official binary not on GitHub → declare `cli.provision` (`github_release` or `http_file`) so fleet sync docker-cps into runners. Host-installed binaries (e.g. Contabo) do **not** replace provision — runners are containers and do not see `/usr/local/bin` from the host unless copied in.

### 2. Packaged manifest

Add `packages/portunus_skill_bridge/manifests/<slug>.example.yaml`.

Required shape:

- `name`, `version`, `backend` (`json_cli` | `http_rpc`)
- `auth` — see [references/auth-and-manifest.md](references/auth-and-manifest.md)
- `cli:` or `http:` matching backend
- `tools:` — **agent tools only** (never expose login/logout/status as MCP tools)
- Optional `cli.provision` — see auth-and-manifest (**Provision**)

Validate locally:

```bash
cd packages/portunus_skill_bridge && uv run python -c \
  'from portunus_skill_bridge.manifest import load_manifest; print(load_manifest("manifests/<slug>.example.yaml").name)'
```

### 3. Seed `DEFAULT_APPS` **and** `catalog/apps/<slug>.toml`

1. In `gateway/portunus_gateway/catalog.py`: `_load_packaged_manifest` (skill_bridge) + append `DEFAULT_APPS`.
2. Add matching `catalog/apps/<slug>.toml` (filename stem = slug). skill_bridge uses
   `spec.manifest = "<slug>.example.yaml"` + `secrets = ["ENV_NAME"]` (not inlined YAML).
   Keep TOML and `DEFAULT_APPS` in parity — `test_packaged_apps_cover_default_apps` enforces it.
`ensure_defaults()` **inserts missing `skill_bridge` / `http` / `oauth` / rclone-stdio seeds**
and refreshes skill_bridge when tools/auth change.

### 4. Dashboard icon

Follow `.cursor/rules/app-icons.mdc` and `DESIGN.md` (Iconography).

1. Asset: `dashboard/public/icons/apps/<slug>.{png|svg}` — **official** full-bleed mark only (logo fills the tile). Never invent a placeholder logo.
2. Prefer the **product** mark (e.g. Drive `apple-touch-icon` from `drive.proton.me`), not a parent-account favicon that looks like another product.
3. Wire `AppIconId`, `appIconIdForSlug`, and `OFFICIAL_ICON_FILES` in `dashboard/src/icons/apps.tsx` **only if** that asset exists (`AppBrandMark` derives kind via `appIconIdForSlug`).
4. Add `.app-brand-image.<slug>` to the **48×48** rule in `dashboard/src/style.css` (default is 30×30 and looks tiny).
5. No redistributable mark yet → omit from `OFFICIAL_ICON_FILES`; initial-letter fallback is correct.

Platform auth UIs (`device_qr` / `oauth_link`) are already generic in `AppsPage.tsx` — no new panel unless you invent a new auth type.

### 5. Tests

- Manifest unit test under `packages/portunus_skill_bridge/tests/`
- Catalog seed assertion in `gateway/tests/test_skill_bridge_catalog.py` when adding a skill_bridge seed

### 6. Deploy + verify

Follow [references/deploy-checklist.md](references/deploy-checklist.md).

Completion criteria for a skill_bridge app:

- [ ] Manifest loads; auth type correct; no login tools in `tools`
- [ ] Catalog row present after `ensure_defaults` (or API create)
- [ ] Icon 48×48 on Apps cards
- [ ] Skill tree + **current** `portunus_skill_bridge` on **all** runners
- [ ] Connect login succeeds (`logged_in`)
- [ ] Probe returns expected tools; server enabled for agents

## Non–skill-bridge path

1. Add `DEFAULT_APPS` entry with the right `connection` + `spec` (see peers in `catalog.py`).
2. Icon steps above.
3. On empty DB: restart gateway → `ensure_defaults` seeds all. On existing DB: create the catalog row via operator API from the same fields (oauth/http/webdav are not auto-inserted later).
4. Connect in UI → Probe → Enable.

## Hard rules

- **Agents never own vendor login.** Platform Connect runs `skill_exec` auth; MCP tools are post-auth only.
- **oauth_link authorize URLs are not QR codes.** Auto-open popup/tab + Open/Copy. Vendor in-app 扫一扫 often treats the URL as a code (`授权码无效`).
- **Browser-only CLI login** (Proton-style): `token_paste: false`; wrapper must feed the printed https URL into `BROWSER`/`xdg-open` so `browser_capture` can surface it. Prefer file-backed credential stores under `STATE_DIR` (e.g. `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file`) — libsecret/keyring often fails inside runner containers.
- **Stale runner `portunus_skill_bridge` fails probe** (manifest rejects new auth fields → `upstream_crash`). Sync the package to gateway **and** every runner after AuthSpec/manifest changes.
- **Omit CLI identity flags** vendors reject (`cli.identity: {}` when needed). Use `auth.login_env` for headless/agent detection instead.
- Set catalog `egress` (`overseas` / `domestic` / `any`) so Connect/probe prefers the right fleet region.
- Do not commit secrets, `.venv`, or `node_modules`.

## References

- Auth knobs + manifest fields: [references/auth-and-manifest.md](references/auth-and-manifest.md)
- Deploy / probe checklist: [references/deploy-checklist.md](references/deploy-checklist.md)
