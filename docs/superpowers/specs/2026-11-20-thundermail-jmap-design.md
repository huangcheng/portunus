# Thundermail (JMAP) — design

**Size:** one first-party catalog app  
**Status:** approved — app password + JMAP (no OAuth)

## Intent

Operators Connect **Thundermail** with email + app password. Agents get mail tools over JMAP at `mail.thundermail.com:443`.

## Approach

- Catalog `thundermail`, `connection: http`, `url: portunus-jmap://thundermail`
- Gateway `JmapBuiltinClient` (same shape as Graph): session + Mail/Submission methods
- Connect: email + app password → vault `Authorization: Basic …` (no Bearer prefix)
- `egress: overseas`
- No third-party MCP; no baked credentials

## Tools (v1)

`list_mailboxes`, `list_messages`, `get_message`, `search_messages`, `send_mail`

## Non-goals

- IMAP/SMTP (JMAP covers send via EmailSubmission)
- OAuth/DCR
- CalDAV/CardDAV
