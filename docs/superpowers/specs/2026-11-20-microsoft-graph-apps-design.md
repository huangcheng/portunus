# Microsoft Graph apps (Outlook Mail + OneDrive) — design

**Size:** two first-party catalog apps sharing one Entra registration  
**Status:** Entra done; **no softaria** — Graph via `portunus-graph://` builtin

## Intent

Operators Connect **Outlook Mail** and **OneDrive** (personal Microsoft accounts). Agents get mail/file tools. One Entra app; two catalog recipes (separate grants / pause). **No third-party MCP packages** (`@softeria/ms-365-mcp-server` rejected).

## Entra (done)

| Field | Value |
|---|---|
| Display name | Portunus |
| App (client) ID | `2cfb730c-0b20-4ddd-8e87-a67a77db4546` |
| Audience | Personal Microsoft accounts only |
| Redirect | `https://portunus.wuhan.dev/api/oauth/callback` |
| Permissions | `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `Files.ReadWrite`, `openid`, `profile`, `offline_access` |
| Client secret | `~/.config/portunus/ms-entra-portunus.json` only (not in git) |

## Approaches

| Approach | Trade-off | Decision |
|---|---|---|
| `@softeria/ms-365-mcp-server` sidecar | Extra npm dependency / ops surface | **Rejected** |
| Official Work IQ Mail MCP | Copilot / no personal MSA | Rejected |
| `skill_bridge` + `http_rpc` → Graph | Possible, but Entra browser OAuth is already `connection: oauth` | Secondary |
| **`portunus-graph://` + catalog oauth** | First-party Graph client; reuses Entra OAuth + vault refresh | **Chosen** |
| New plugin framework | Catalog already extensible | Rejected |

## Goals

- Catalog `outlook-mail` (new) and `onedrive` (replace rclone) as **`connection: oauth`**.
- Spec URL is `portunus-graph://mail` or `portunus-graph://onedrive`; gateway `RemoteProxy` dispatches to `GraphBuiltinClient` (in-process Graph HTTP).
- Auth: Portunus-owned Entra OAuth (baked `client_id`; operator pastes client secret at Connect) → vault access/refresh → `Authorization: Bearer` on Graph.
- `egress: overseas`.
- `ensure_defaults` migration: rclone OneDrive → oauth Graph when seeds ship.

## Non-goals

- Third-party MCP runtimes or compose sidecars for Graph.
- Work/school / Copilot Work IQ (v1).
- One combined mail+files Connect card.
- Committing the client secret.

## Constraints

- Prefer remote MCP when the **vendor** hosts one (`AGENTS.md`); Graph has no personal hosted MCP → first-party Graph adapter is correct.
- Agents never own vendor login.
- Built-in apps may special-case a URL scheme; do not invent a separate plugin ABI.
