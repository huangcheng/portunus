---
name: portunus
description: >
  Use Portunus — the MCP gateway at this origin. Connect agents to /mcp with a
  Bearer key, discover granted servers/tools, and invoke tools through the four
  portunus_* meta tools. Use when wiring an agent to Portunus, calling upstream
  MCP apps through the gateway, or debugging discover/invoke failures.
---

# Portunus

Portunus is a self-hosted **MCP gateway**. Operators connect apps (catalog or custom) and vault secrets in the dashboard. Agents never see those secrets — they talk only to **one Streamable HTTP endpoint** with a scoped Bearer key.

**This host:** MCP is at `/mcp` on the same origin as this skill (production: `https://portunus.wuhan.dev/mcp`).

## Connect the agent

1. Get a key from the operator (**Keys** in the dashboard). It looks like `pm-…` and is shown once.
2. Register this MCP server in the agent config (URL + Bearer). Prefer an env var or credential store for the token — not a synced plaintext config.
3. Send a non-empty **`User-Agent`** on every request. Edge proxies (e.g. Cloudflare) may return **403** without one.
4. Confirm the four meta tools appear: `portunus_discover_servers`, `portunus_discover_tools`, `portunus_get_tool_schema`, `portunus_invoke_tool`.

### Client snippets

Cursor / Copilot (`.cursor/mcp.json` / `.vscode/mcp.json`):

```json
{
  "mcpServers": {
    "portunus": {
      "url": "https://portunus.wuhan.dev/mcp",
      "headers": {
        "Authorization": "Bearer ${env:PORTUNUS_AGENT_KEY}"
      }
    }
  }
}
```

HTTP shape: Streamable HTTP MCP at `/mcp`, `Authorization: Bearer pm-…`.

## How to call tools

Always go through the meta tools. Upstream app tools are **not** listed at the top level.

1. **`portunus_discover_servers`** — list servers this key may use (paged). Check `status` (`ok` | `auth_required` | `unreachable` | `probing`) and `tool_count`.
2. **`portunus_discover_tools`** with `{ "server": "<slug>" }` — list enabled tools on one server.
3. **`portunus_get_tool_schema`** with `{ "server", "tool" }` — fetch input schema + `schema_hash` when arguments are unclear.
4. **`portunus_invoke_tool`** with `{ "server", "tool", "arguments": { … } }`. Pass `schema_hash` from discover/get when you have it so a drifted schema returns `schema_drift` instead of a silent mismatch.

Paging: `query`, `cursor` (`next_cursor` from the previous page), `limit` (default 50, max 100).

### Schemas

Meta-tool JSON Schema fields use **`input_schema`** (snake_case), not MCP wire `inputSchema`.

## Rules

- Only **granted + enabled** servers/tools are visible. Missing server → ask the operator to Connect, Probe, enable, and grant the key.
- `auth_required` / missing vault secrets → operator must finish Connect (OAuth, token, or skill login). Agents cannot paste vendor credentials into Portunus.
- Do not invent parallel HTTP calls to upstream MCP URLs. Invoke through Portunus.
- Treat the agent key as a secret. On `unauthorized` / revoked key, stop and ask for a new key.

## Failures worth knowing

| Signal | Meaning |
|---|---|
| `403` with empty/HTML body | Often missing `User-Agent` at the edge |
| `unauthorized` | Bad, revoked, or missing Bearer key |
| `auth_required` | Server needs operator Connect / re-auth |
| `schema_drift` | Re-fetch schema; update arguments / `schema_hash` |
| `upstream_timeout` | Read-only / idempotent tools may be retried once by the gateway |
| `upstream_crash` | Retried once only when the stdio child actually exited; JSON-RPC/tool errors are not auto-retried |
| `payload_too_large` | Lower result size or set `truncate: true` on invoke |

## Operator boundary

Dashboard **Apps · Keys · Fleet · Logs · Workflows** is for humans. Agents use `/mcp` only. Connecting new apps, minting keys, and vaulting secrets is out of band — ask the operator.
