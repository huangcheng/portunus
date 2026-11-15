# iDrive Cloud Drive (not E2)

Status: **not shipped as a Portunus catalog app yet**. Contabo already runs the
vendor Cloud Drive sync stack; Portunus still lacks a headless, fleet-wide
file API to wrap.

## Contabo facts (re-checked 2026-11-15)

| Piece | Path / unit | Notes |
|---|---|---|
| Official Linux CLI | `/opt/IDriveForLinux` **v3.16.0**, `/usr/local/bin/idrive` | Interactive menu |
| **Menu 12 — Cloud drive** | `menu_cloud_drive` | Present in the CLI (and in your screenshot). Older Contabo note claiming “no Cloud Drive entry” was **wrong** for this build. |
| What 12 does | launches Electron `cloud-drive` + stores a Cloud Drive session | Not a browsable JSON/file MCP; sets up sync |
| Sync root | `/root/Cloud-Drive` | Local mirror after daemon is up |
| Daemon | `idrive-clouddrive.service` → `cloud-drive … run-daemon` | Host-local continuous sync |
| Backup product | menu 1–11 / `--about` | Separate quota from Cloud Drive |

Locale strings in the binary also cover session store, API request/response, and
“Cloud drive is not configured. Please setup…” — so 12 is real setup/ops UI,
not a stub label.

## Why Portunus still cannot Connect it

1. Menu 12 is **interactive** (login / encryption key / confirmations). No
   documented headless token paste for Cloud Drive (unlike rclone remotes).
2. After setup, agents would only see a **local folder on Contabo**, not a
   remote drive reachable from vultr/tencent runners.
3. Personal Cloud Drive still has **no official rclone backend / WebDAV**;
   **E2** remains a different S3 product (out of scope).

## If this becomes shippable

Need either: headless Cloud Drive session + a stable file API, a real rclone
backend, or a documented EVS/WebDAV path. Then wire like Box/OneDrive
(`deploy/rclone-shim` + `DEFAULT_APPS` stdio seed).
