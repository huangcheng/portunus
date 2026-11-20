# Agent Mail (腾讯 Agently) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Portunus catalog app `agent-mail` (skill_bridge + json_cli around `@tencent-qqmail/agently-cli`) with browser-only OAuth Connect, plus install/authorize the CLI on the operator machine.

**Architecture:** Proton Drive peer: Node wrapper under `third_party/agent-mail/` speaks Portunus auth JSON (`oauth_link` + `browser_capture` + `token_paste: false`) and maps MCP tools to `agently-cli` subcommands via `npx` (no `cli.provision` — npm-only binary, same pattern as FlyAI). Seed `DEFAULT_APPS` + TOML; domestic egress.

**Tech Stack:** Node wrapper, `@tencent-qqmail/agently-cli` (pin latest stable inspected at implement time), packaged YAML manifest, gateway catalog, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-agent-mail-design.md`

## Global Constraints

- Auth: `oauth_link`, `browser_capture: true`, `token_paste: false`, pending `-106`/`-114`, success `0`.
- Never expose login/logout/status as MCP tools; never rewrite authorize URLs; never retry failed `auth login`.
- Slug `agent-mail`; `egress: domestic`; `SKILL_ROOT=/var/lib/portunus/skills/agent-mail`; `STATE_DIR=/var/lib/portunus/skill-state/agent-mail`.
- Wrapper must feed captured https URL to `BROWSER`/`xdg-open` for Connect capture.
- Prefer `HOME`/`XDG_*` under `STATE_DIR` so session files survive in runners (builder confirms CLI config path after install).
- Follow `.cursor/skills/add-portunus-app/SKILL.md`; no invented icons.

## Review Focus

- Authorize URL treated as opaque (no encode/QR) — wrapper `extractUrl` + Connect popup only.
- Browser completes alone (`token_paste: false`) — login-status polls until `+me` succeeds.
- Mutation tools require optional `confirmation_token` for vendor two-step confirm.
- `npx` pin fails closed with clear stderr if package missing/network blocked on runner.
- Domestic region preference so Connect does not stick on overseas runners.

---

### Task 1: Packaged manifest + unit tests

**Files:**
- Create: `packages/portunus_skill_bridge/manifests/agent-mail.example.yaml`
- Modify: `packages/portunus_skill_bridge/tests/test_manifest.py`
- Test: same

**Interfaces:**
- Produces: manifest `name: agent-mail`, `backend: json_cli`, auth as Global Constraints, `cli.command: ["node", "scripts/agent-mail.cjs"]`, `identity: {}`, `run_wrapper: raw`, no `provision`
- Tools (exact names): `me`, `message_list`, `message_read`, `message_search`, `message_send`, `message_reply`, `message_forward`, `message_trash`, `message_delete`, `attachment_upload`, `attachment_download`
- Mutation tools include optional string input `confirmation_token`

- [ ] **Step 1: Write failing test `test_load_agent_mail_example`**

Assert: oauth_link + browser_capture + `token_paste is False`; command as above; tool set includes listed names; `"login" not in tool names`.

- [ ] **Step 2: Run test — expect FAIL (file missing)**

Run: `cd packages/portunus_skill_bridge && uv run pytest tests/test_manifest.py::test_load_agent_mail_example -v`

- [ ] **Step 3: Add `agent-mail.example.yaml`**

Map CLI verbs: `+me` → `me`; `message +list|read|search|send|reply|forward|trash|delete`; `attachment +upload|+download`. Wire repeatable `--to/--cc/--bcc` as array-or-string inputs the wrapper will expand. `connect_hint` mentions agent.qq.com WeChat/OAuth browser login.

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit** (only if user asked to commit; otherwise skip)

---

### Task 2: Node wrapper skill tree

**Files:**
- Create: `third_party/agent-mail/scripts/agent-mail.cjs`
- Create: `third_party/agent-mail/VERSION`
- Modify: `third_party/README.md`
- Modify: `deploy/fleet-skills.sh` (`state_slug_for` / `entrypoint_for` for `agent-mail`)

**Interfaces:**
- Consumes: `AGENT_MAIL_BIN` (optional absolute/npx argv0) else `npx -y @tencent-qqmail/agently-cli@<pin>`
- Auth actions: `login` | `login-status` | `logout` → Portunus `{code,msg,action,type:"result",data}`
- `login`: spawn `auth login`, extract first https URL, `openBrowser`, emit pending `-106` + `data.auth_url` until process exits 0
- `login-status`: run `+me` (or `auth status`); code `0` when mailbox identity present
- Tools: print CLI stdout (prefer `--json` if CLI supports it; else pass-through text in JSON envelope `{ok, raw}`)

- [ ] **Step 1: Inspect installed CLI** (`npm i -g` locally if needed) for flags: `--json`, auth status, config dir, confirmation-token flag name

- [ ] **Step 2: Implement wrapper** mirroring `third_party/proton-drive/scripts/proton-drive.cjs` auth path; map tool argv per manifest `cli:` fields

- [ ] **Step 3: Smoke auth JSON shape**

Run: `node third_party/agent-mail/scripts/agent-mail.cjs login-status` (expect not-logged-in JSON, exit ≠ 0)

- [ ] **Step 4: Wire fleet-skills + README**

- [ ] **Step 5: Commit** (if requested)

---

### Task 3: Catalog seed + TOML + icon wiring

**Files:**
- Modify: `gateway/portunus_gateway/catalog.py` (`AGENT_MAIL_MANIFEST`, `DEFAULT_APPS` entry, `DOMESTIC_EGRESS_SLUGS`)
- Create: `catalog/apps/agent-mail.toml`
- Modify: `gateway/tests/test_skill_bridge_catalog.py` (peer `test_validate_proton_drive_auth_seed`)
- Modify: `dashboard/src/icons/apps.tsx` + `dashboard/src/style.css` **only if** official asset lands under `dashboard/public/icons/apps/agent-mail.{png,svg}`

**Interfaces:**
- Seed `connection: skill_bridge`, `egress: domestic`, `categories: ["Productivity"]` or `["Communication"]` (match existing mail-like apps), `tags: ["tencent", "mail", "json_cli", "oauth_link"]`
- `spec.manifest` = loaded packaged YAML; `secrets: []`; `env_defaults` with `SKILL_ROOT` / `STATE_DIR` / optional `AGENT_MAIL_BIN` / `HOME`→`STATE_DIR` if CLI stores under `$HOME`

- [ ] **Step 1: Write `test_validate_agent_mail_auth_seed`** — assert domestic preferred regions, oauth_link auth, env_defaults paths

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Seed catalog.py + TOML in parity**

- [ ] **Step 4: Run catalog + packaged-apps coverage tests — PASS**

Run: `cd gateway && uv run pytest tests/test_skill_bridge_catalog.py -k agent_mail -v` (and packaged cover if separate)

- [ ] **Step 5: Icon** — fetch official mark from agent.qq.com if redistributable; else leave initial-letter fallback

---

### Task 4: Local CLI install + OAuth (operator machine)

**Files:** none (machine state)

- [ ] **Step 1:** `npm install -g @tencent-qqmail/agently-cli`

- [ ] **Step 2:** Background+pty `agently-cli auth login`; extract opaque URL; show  
  `请点击或复制以下链接在浏览器中完成授权：`  
  and a fenced block with the raw URL only. Do not modify URL. Do not retry on failure/timeout.

- [ ] **Step 3:** After process exits 0, `agently-cli +me` and print success template with real address.

- [ ] **Step 4:** Optional skill install (`npx skills add https://agent.qq.com --skill -g -y`) — operator local agents only; not required for Portunus.

---

### Task 5: Deploy checklist (ops)

**Files:** none required beyond prior tasks

- [ ] Sync skill tree + `portunus_skill_bridge` to gateway **and** all runners (`fleet-skills` / providers-update per `references/deploy-checklist.md`)
- [ ] Connect in dashboard → browser login → probe tools → enable
- [ ] Confirm completion criteria from add-portunus-app skill

---

## Spec coverage

| Spec item | Task |
|---|---|
| skill_bridge + json_cli | 1–3 |
| oauth_link browser-only | 1–2 |
| Tool list + confirmation_token | 1–2 |
| TOML + DEFAULT_APPS | 3 |
| Local install + auth | 4 |
| Fleet sync | 2, 5 |
| Icon if available | 3 |
