# Microsoft Graph apps (Outlook Mail + OneDrive) — design

**Size:** two first-party catalog apps sharing one Entra registration pattern  
**Status:** Graph via `portunus-graph://` builtin; operators bring their own Entra app

## Intent

Operators Connect **Outlook Mail** and **OneDrive** (personal Microsoft accounts). Agents get mail/file tools. Two catalog recipes (separate grants / pause). **No third-party MCP packages**. **No baked Client ID** — self-hosted OSS; each deploy registers its own Entra app (same pattern as GitHub / Dropbox / Google Drive).

## Operator Entra app

| Field | Guidance |
|---|---|
| Audience | Personal Microsoft accounts (consumers) for v1 |
| Redirect | This gateway's `/api/oauth/callback` |
| Permissions | Mail app: `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `openid`, `profile`, `offline_access`. OneDrive: `User.Read`, `Files.ReadWrite`, plus openid/profile/offline_access |
| Connect | Paste **Client ID** + **client secret**, then Authorize |

## Approaches

| Approach | Trade-off | Decision |
|---|---|---|
| `@softeria/ms-365-mcp-server` sidecar | Extra npm dependency / ops surface | **Rejected** |
| Official Work IQ Mail MCP | Copilot / no personal MSA | Rejected |
| Baked Portunus Entra `client_id` | Not portable for self-host | **Rejected** |
| **`portunus-graph://` + catalog oauth** | First-party Graph client; operator-pasted Entra credentials | **Chosen** |

## Goals

- Catalog `outlook-mail` and `onedrive` as **`connection: oauth`** with no `spec.client_id`.
- Spec URL is `portunus-graph://mail` or `portunus-graph://onedrive`; gateway `RemoteProxy` dispatches to `GraphBuiltinClient`.
- Auth: operator Entra OAuth → vault access/refresh → `Authorization: Bearer` on Graph.
- `egress: overseas`.
- `ensure_defaults` migrates rclone OneDrive → oauth Graph and strips any previously baked client_id from catalog rows.

## Non-goals

- Third-party MCP runtimes or compose sidecars for Graph.
- Work/school / Copilot Work IQ (v1).
- One combined mail+files Connect card.
- Shipping a Portunus-owned client id or secret.

## Constraints

- Prefer remote MCP when the **vendor** hosts one (`AGENTS.md`); Graph has no personal hosted MCP → first-party Graph adapter is correct.
- Agents never own vendor login.
- Built-in apps may special-case a URL scheme; do not invent a separate plugin ABI.
