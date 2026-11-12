# Security

## Reporting

Report vulnerabilities via GitHub Security Advisories on this repository.
Do not open public issues that include exploit details, credentials, or private infrastructure.

## Do not commit

Never commit:

- `.env`, `.env.*` (except a placeholder `.env.example` with fake values)
- `secrets/`, pepper files, runner tokens, vault keyrings
- Operator passwords, agent keys (`pm-…`), session cookies
- Private hostnames, inventory, deploy runbooks, or personal automation that names real accounts
- CI secrets, cloud credentials, or TLS private keys

Local compose material under `secrets/` and generated `.env` are gitignored.
Rotate anything that was ever pasted into chat, screenshots, or logs.

## Design notes

- Agent keys are shown once at mint. Verification uses an HMAC digest of the secret;
  a reversible AES-GCM copy (`secret_ciphertext`) is also stored so operators can
  re-reveal the token via `GET /api/keys/{id}/secret` (audited). Treat the pepper
  and vault keyring as highly sensitive — together they can recover every stored key.
- Vault seals env literals / Authorization headers with AES-GCM; plaintext must not
  appear in list API responses, audit rows, or error messages.
- Dev mode (`PORTUNUS_DEV=1`) is for local evaluation only.
- Public `GET /healthz` only reports liveness / whether any runner is connected.
  Fleet counts are operator-authenticated (`GET /api/status`).

## MCP authorization posture (deliberate deviation)

The `/mcp` endpoint authenticates agents with operator-issued bearer keys (`pm-…`),
not OAuth 2.1. MCP 2026-07-28 allows this: authorization is optional and parties "MAY
negotiate their own custom authentication and authorization strategies".

Consequences, recorded so the deviation is a decision and not an oversight:

- `/.well-known/oauth-protected-resource` (RFC 9728) is served so standards-following
  clients can discover the challenge shape (Bearer header, no scopes). The document
  intentionally omits `authorization_servers`: there is no authorization server, and
  the MCP authorization requirement to list at least one applies only to servers
  implementing the OAuth framework itself. RFC 9728 marks the field optional.
- `WWW-Authenticate` challenges on 401 carry `resource_metadata` but no `scope`
  (the key model has no scopes; see RFC 6750 §3, scope is SHOULD).
- The gateway's *client-side* OAuth (connecting remote MCP servers) implements
  authorization-code + PKCE S256, RFC 8707 `resource`, and RFC 9207 `iss` response
  validation (exact when an explicit `issuer` is configured, origin-anchored to the
  authorize endpoint otherwise — full exact comparison requires AS metadata discovery,
  which the operator-configured flow deliberately does not perform).
