# Portunus — agent instructions

## Commits are gated, mechanically

Every commit (agent or human) runs `ocr review --audience agent` first. Do **not** chain
`ocr review … && git commit`: the chain commits on the command's exit code, not on the
findings — findings have been committed unread that way more than once.

Use the wrapper, which counts findings and refuses to commit unless they are zero or an
explicit reason is recorded in the commit body:

    scripts/gated-commit.sh -m "subject" -m "body"
    scripts/gated-commit.sh -m "subject" --allow "finding N is X because Y"

If `ocr` is missing or broken, say so and stop — do not commit around the gate.

## Catalog apps need TOML

Every public Provider / App that operators can Connect must have a recipe file at
`catalog/apps/<slug>.toml` (filename stem = slug), kept in parity with any
`DEFAULT_APPS` seed in `gateway/portunus_gateway/catalog.py`.

`test_packaged_apps_cover_default_apps` enforces the mapping. When adding an app,
follow `.cursor/skills/add-portunus-app/SKILL.md` (manifest + seed + TOML + icon).

## Prefer remote API / MCP endpoints over CLIs

When wiring a vendor into the catalog, **always prefer a hosted remote MCP (HTTP /
SSE / Streamable HTTP) or HTTP API** over a local CLI / `stdio` / `uvx` / `npx`
proxy when the vendor offers one.

Order of preference:

1. **Remote MCP + OAuth** (`connection = "oauth"`) — fixed or operator-pasted URL
2. **Remote MCP + bearer / API token** (`connection = "http"`, optionally
   `url_from_operator`)
3. **HTTP RPC skill-bridge** (`skill_bridge` + `http_rpc`) when there is a stable
   HTTP API but no MCP
4. **Local stdio / CLI** (`stdio`, `uvx` packages, `skill_bridge` + `json_cli`) —
   only when no usable remote endpoint exists

Do not keep a local mcp-proxy / CLI wrapper if a remote endpoint is available.
Product-scoped hosted marketplace URLs (e.g. Tencent MCP 广场 SSE) count as
remote and beat stdio. Document how the operator gets the URL or token in
`setup_hints`.

**阿里云** remote OAuth cannot use DCR: `/register` always returns the official
client and ignores custom `redirect_uris` (→ `redirect_uri_mismatch` for
Portunus). Use Custom OAuth — operator pastes a RAM OAuth 2.1 Web/Native Client
ID (no secret; PKCE) registered with Portunus’s callback, and sets Core →
自定义 OAuth to that app.
