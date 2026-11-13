# Portunus — Schema Override and Probe Findings

**Date:** 2026-11-13  
**Status:** design  
**Covers:** black-box MCP probe findings (Quark, Guangyapan diagnostics, WebDAV timeouts, dida365 schema drift) via operator schema overrides and gateway reliability fixes.  
**Related:** `2026-11-09-portunus-design.md`, `2026-11-11-server-state-design.md`.

## 1. Problem

A read-only probe of `portunus.wuhan.dev/mcp` showed:

- Quark is unusable: upstream agent-environment gate (`-104`); re-auth does not help.
- Dropbox / PikPak need OAuth reconnect (ops).
- Guangyapan failures surface as generic `Error executing tool …` with no stderr.
- dida365 `query_calendar_events` advertises optional `query` but upstream requires it.
- WebDAV (nutstore/koofr) occasionally `upstream_timeout`.

## 2. Decisions

1. **Operator schema override (not catalog hard-codes).** Nullable `tools.schema_override`. Probed schema stays in `tools.schema`. Effective schema = override if set, else probed. `schema_hash` always hashes the effective schema. Probe never clears an override.
2. **One automatic retry** on `upstream_timeout` only when the tool is read-only / idempotent (`readOnlyHint` / `idempotentHint`, or known WebDAV list/stat/read tools), and the caller did not shorten the timeout. The first timeout is recorded for site health; per-attempt invoke attrs are cleared before the retry.
3. **Skill-bridge handlers** catch exceptions and return a JSON CLI envelope (`exit_code`, `message`/`stderr`) so Omni can promote a useful `upstream_error`.
4. **Quark honesty:** catalog description + `popular: false`; optional clearer `-104` message. No OpenClaw / agent-env spoof.
5. **Dropbox / PikPak / Guangyapan health** on production remain ops (reconnect, skill roots, SSL).

## 3. Effective schema

| Field | Meaning |
|---|---|
| `schema` | Last successful probe `inputSchema` |
| `schema_override` | Operator JSON Schema object, or null |
| `schema_hash` | SHA-256 of canonical effective schema |

Operator API:

- `PUT /api/tools/{id}/schema-override` `{ "schema": {…} }`
- `DELETE /api/tools/{id}/schema-override`

`tool_view` returns effective `schema` + `schema_hash`, `schema_overridden`, and `probed_schema` when overridden.

Omni `discover_tools` / `get_tool_schema` / invoke validation / remote `Mcp-Param-*` all use the effective schema.

## 4. dida365 path

After ship, the operator sets an override for `query_calendar_events` that marks `query` (and nested fields) required so agents get `invalid_tool_arguments` from Portunus instead of a remote Pydantic error.

## 5. Client notes

- Omni meta-tools use `input_schema` (snake_case), not wire `inputSchema`.
- Cloudflare may `403` requests without `User-Agent`.
- Treat agent keys as secrets; prefer env/file references over inline headers.

## 6. Out of scope

Mutating-tool coverage; fabricated IDs for skipped read-only tools; Quark spoofing; automatic per-app schema patches; changing edge User-Agent policy beyond docs.
