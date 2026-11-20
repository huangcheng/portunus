# Himalaya IMAP/SMTP providers — design

**Status:** implemented (shared himalaya-shim + `qqmail-imap` provider)

## Intent

Operators see **separate Connect cards per mail provider** (QQ Mail, …). Runners ship **one** Himalaya install. Each provider recipe is a catalog TOML that only differs in mailbox endpoints, labels, and setup hints — same shim, same binary.

## Shape

```
catalog/apps/qqmail-imap.toml   →  env_defaults: imap.qq.com / smtp.qq.com
catalog/apps/<provider>-imap.toml →  env_defaults: that vendor’s hosts
        │
        ▼
command = /var/lib/portunus/skills/himalaya-shim/shim   (shared)
        │
        ▼
one himalaya binary beside the shim on every runner
```

### Shared backend (once)

| Piece | Path / role |
|-------|-------------|
| Shim | `/var/lib/portunus/skills/himalaya-shim/shim` |
| Binary | `/var/lib/portunus/skills/himalaya-shim/himalaya` (provisioned once) |
| Policy | shim on `binary_allowlist` (like rclone-shim) |

Shim (`run`) reads vault env, writes a 0600 one-account Himalaya config, and is
invoked by skill-bridge as the vendor CLI (`himalaya --json -c <cfg> …`).
Released Himalaya v2.2.1 has no `mcp` subcommand yet (still unreleased upstream);
skill-bridge + `--json` is the durable MCP surface until that ships.

Child env must not inherit `MAIL_PASSWORD`.

### Per-provider catalog TOML

Same `connection = "stdio"` / same `command` / same env **names**; differ by:

- `slug`, `label`, `description`, `tags`, `egress`, `setup_hints`
- `spec.env_defaults` for `IMAP_URL` / `SMTP_URL` (and optional mailbox aliases later)

Connect fields (all providers):

| Name | Secret | Who sets it |
|------|--------|-------------|
| `IMAP_URL` | no | recipe `env_defaults` (operator can override) |
| `SMTP_URL` | no | recipe `env_defaults` |
| `MAIL_ADDRESS` | no | operator |
| `MAIL_PASSWORD` | yes | operator (app password / 授权码) |

### First provider: `qqmail-imap`

- Defaults: `imaps://imap.qq.com:993`, `smtps://smtp.qq.com:465`
- Hints: enable IMAP/SMTP, create 授权码 — not login password
- Official `qqmail` remote MCP paste is **delisted** (Workbuddy-oriented; too brittle)

Later providers are new TOML + `DEFAULT_APPS` rows only — no new binary, no new shim.

## Non-goals

- One mega “IMAP / SMTP” card with blank hosts as the only UX (providers stay separate)
- Per-provider himalaya installs
- OAuth / XOAUTH2 / JMAP / Graph inside the shim (other catalog apps)
- mutt
- Multiple Himalaya accounts per Connect

## Why not paste full Himalaya TOML

Operator UX is address + password; hosts live in the recipe. Advanced override via visible `IMAP_URL` / `SMTP_URL` fields is enough.
