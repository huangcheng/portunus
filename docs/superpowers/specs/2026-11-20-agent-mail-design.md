# Agent Mail (腾讯 Agently) — design

**Size:** small catalog app (peer: Proton Drive)  
**Status:** approved

## Intent

Add Tencent **Agent Mail** (`agent.qq.com` / Agently) so operators can Connect it in Portunus and agents can list/read/search/send mail. Also install and authorize the official CLI on this machine for the operator.

## Approaches

| Approach | Trade-off | Decision |
|---|---|---|
| **skill_bridge + json_cli** wrapping `@tencent-qqmail/agently-cli` | Standard when no remote MCP; Connect owns OAuth | **Chosen** |
| Remote MCP URL | No official public MCP for Portunus | Rejected |
| Local CLI only | No catalog / no fleet agents | Out of scope as sole deliverable |

## Goals

- Catalog app `agent-mail` (slug): Connect → browser OAuth → probe tools → enable for agents.
- Auth: `oauth_link` + `browser_capture: true` + `token_paste: false` (CLI prints URL; browser completes; no paste code).
- MCP tools are post-auth only (no login/logout/status as tools).
- Local: install `agently-cli`, run `auth login` per vendor docs (opaque URL, no retry), verify with `+me`.

## Non-goals

- Unofficial third-party MCP gateways.
- Exposing confirmation-token UX beyond what the wrapper can pass through as tool args.
- Hosting or re-implementing QQ Mail APIs.

## Anti-goals

- Treating the authorize URL as a QR / rewriting the URL.
- Retrying failed/timed-out `auth login`.
- Agents owning vendor login.

## Constraints

- Prefer remote MCP over CLI (`AGENTS.md`); none available → CLI is correct.
- `egress: domestic` (Tencent / QQ Mail).
- Runner needs Node wrapper + provisioned `agently-cli` (npm global or `npx`/`http_file`-style provision — builder picks peer that works in containers).
- Session/credentials under `STATE_DIR` so container keyrings are not required.
- Catalog TOML + `DEFAULT_APPS` parity; packaged manifest; icon only if redistributable mark exists.

## How (decided)

1. Packaged manifest `agent-mail.example.yaml` + seed + `catalog/apps/agent-mail.toml`.
2. `third_party/agent-mail/` Node wrapper (Proton-style): auth verbs emit Portunus JSON; tools shell out to `agently-cli` with JSON-friendly output.
3. Tools (v1): `me`, `message_list`, `message_read`, `message_search`, `message_send`, `message_reply`, `message_forward`, `message_trash`, `message_delete`, `attachment_upload`, `attachment_download` — map 1:1 to CLI; mutation tools accept optional `confirmation_token` for the vendor’s two-step confirm.
4. Fleet: register skill tree + CLI provision; sync runners before Connect.
5. Dashboard icon: official mark if we can redistribute; else initial-letter fallback.
6. Local operator path (this machine): `npm i -g @tencent-qqmail/agently-cli` → background+pty `auth login` → show raw URL with required Chinese prompt → `+me`.

## Left to the builder

- Exact CLI JSON flags / stdout parsing after inspecting installed CLI.
- Provision mechanism (`npx` pin vs absolute bin + fleet provision).
- Whether send/reply need a dedicated “preview then confirm” tool split or a single tool with `confirmation_token`.
- Icon asset sourcing.

## Approval

Approve this design to proceed to an implementation plan, then build + local CLI auth.
