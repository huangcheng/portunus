# Yandex Disk + Yandex Mail (official MCP + OAuth) — design

**Status:** implemented — browser OAuth; drop rclone Disk

## Intent

- **Yandex Disk** (`yandex`): migrate rclone → official remote MCP + operator Entra-style OAuth (own Yandex OAuth app).
- **Yandex Mail** (`yandex-mail`): new catalog app, same pattern.
- Separate Connect cards / grants / scopes. No baked client ids.

## Endpoints

| App | MCP URL |
|---|---|
| Disk | `https://mcp.360.yandex.net/v1/disk/mcp` |
| Mail | `https://mcp.360.yandex.net/v1/mail/mcp` |

Auth header for MCP: `Authorization: OAuth <access_token>` (not Bearer).

OAuth AS: `https://oauth.yandex.ru/authorize` + `https://oauth.yandex.ru/token`  
(`token_endpoint_auth_method: client_secret_post`; refresh supported).

## Catalog

- `connection: oauth`, operator pastes Client ID + secret at Connect, Authorize.
- Disk scopes: `cloud_api:disk.app_folder` avoided; use `cloud_api:disk.read`, `cloud_api:disk.write`, `cloud_api:disk.info`.
- Mail scopes: message/folder/label read+write as required by Mail MCP (no send via MCP — document in hints).
- `authorization_scheme: "OAuth"` so vaulted Authorization uses that scheme after code/refresh exchange.
- Authorize `scope` is space-separated (Yandex ID docs); do not use commas.
- `egress: overseas`.
- `ensure_defaults`: rclone stdio → oauth for `yandex`; insert `yandex-mail`.

## Non-goals

- rclone shim for Yandex
- Baked Portunus client_id
- First-party Disk REST / IMAP while official MCP exists
