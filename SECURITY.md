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
