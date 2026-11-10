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

- Agent keys are shown once at mint; digests only are stored.
- Vault seals env literals with AES-GCM; plaintext must not appear in API responses, audit rows, or error messages.
- Dev mode (`PORTUNUS_DEV=1`) is for local evaluation only.
