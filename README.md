# Portunus

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Self-hosted **MCP gateway**: register stdio or remote MCP servers, keep secrets in an encrypted vault, mint scoped agent keys, and expose everything through one Streamable HTTP endpoint (`/mcp`, four `portunus_*` meta tools).

Operator console covers **Apps · Keys · Fleet · Logs · Workflows**. Design notes live under `docs/superpowers/specs/`.

| Piece | Role |
|---|---|
| `gateway/` | FastAPI: operator API (`/api`), dashboard (`/`), Omni (`/mcp`), vault, keys/grants, probes, embedded Temporal probe worker |
| `runner/` | Sole-client Unix/TLS listener that spawns/reaps stdio children |
| `packages/portunus_shared/` | Argv grammar, wire codec, shared error codes |
| `packages/portunus_worker/` | Temporal control plane: shared OAuth refresh, server probes, connect-ensure, fleet skill sync |
| `dashboard/` | React operator UI (built to `dashboard/dist`, served by the gateway) |
| `catalog/` | Optional TOML app recipes (file-backed Connect catalog) |
| `deploy/` | Runner policy examples, rclone shim, fleet skill helpers |

## Features

- **One Omni endpoint** — agents call `portunus_discover_*` / `portunus_invoke_tool` with a Bearer key
- **Vaulted secrets** — env/headers sealed under `PORTUNUS_SECRETS_KEY`; never returned raw on read
- **Stdio + remote MCP** — `npx`/`uvx`/allowlisted binaries locally; HTTP remotes with operator OAuth (code+PKCE) or paste-token
- **Fleet runners** — multi-site pool with preferred regions, latency-aware pick, and in-probe failover on network / `upstream_crash` failures
- **Temporal workflows** (optional compose profile) — shared `server-probe` and `oauth-refresh` loops (per-server activities so one hung job cannot stall the queue), connect-ensure; probes honor overseas `preferred_regions` + learned site health
- **Operator account** — password + optional Cloudflare Turnstile, TOTP, and passkeys

## Quickstart (local, dev mode)

Needs `uv`, `python3`, `curl`. Dev mode (`PORTUNUS_DEV=1`) skips the privilege drop and uid checks;
**never use it for anything but local evaluation.**

```bash
# Prove the whole path (throwaway runner + gateway, then cleanup)
./scripts/e2e_stdio_omni.sh
```

The script logs in as operator, mints agent keys, registers a fixture stdio MCP server, probes and enables it, grants the key, then discovers and invokes through `/mcp`. It also checks that ungranted / disabled / bad / revoked keys are refused.

To run the services yourself:

```bash
cd gateway
export PORTUNUS_DEV=1
export PORTUNUS_SOCKET=/tmp/portunus/runner.sock       # mkdir -p /tmp/portunus
export PORTUNUS_TOKEN_FILE=/tmp/portunus/runner.token   # any non-empty secret
export PORTUNUS_POLICY=/tmp/portunus/runner-policy.json # cp ../deploy/runner-policy.json.example ...
export PORTUNUS_PEPPER_FILE=/tmp/portunus/pepper        # base64 of >= 32 random bytes
export PORTUNUS_SECRETS_KEY="k1:$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"
export PORTUNUS_OPERATOR_PASSWORD='choose-one'

uv run portunus-runner &          # runner
uv run python -m portunus_gateway # gateway on http://127.0.0.1:8080 (migrates DB on start)
```

Keep `PORTUNUS_SECRETS_KEY` and the pepper file: losing the key makes vaulted env unreadable, and
changing the pepper invalidates every agent key.

## Quickstart (docker compose)

```bash
./scripts/init_env.sh            # writes .env + secrets/{pepper,runner.token}; prints the operator password
docker compose up --build        # gateway + runner on http://localhost:8080

# optional Postgres:
#   set WITH_POSTGRES=1 and PORTUNUS_DATABASE_URL=postgresql+asyncpg://portunus:portunus@postgres:5432/portunus
#   then: docker compose --profile postgres up --build

# optional Temporal (OAuth refresh + probe workflows + UI on :8233):
#   docker compose --profile temporal up --build
#   set TEMPORAL_HOST=temporal:7233 on the gateway service (compose profile wires the worker)
```

The gateway is published on **127.0.0.1 only** (`PORTUNUS_PORT` changes the port, not the interface). Put a TLS reverse proxy in front rather than widening the bind in dev mode.

**Secret file modes.** `secrets/` is `0700` and `.env` is `0600`. `secrets/pepper` and `secrets/runner.token` are
`0644` because compose bind-mounts keep host modes and the container user (uid 10001) must read them; host
users other than you cannot reach them through the `0700` directory. For stricter modes, `chmod 0600` them and run as your own uid with matching volume ownership, or use Docker/Swarm secrets.

Services: `portunus` (gateway, dashboard, `/mcp`), `portunus-runner` (no published port; socket volume only),
`postgres` (profile `postgres`), `temporal` / `temporal-ui` / `portunus-worker` (profile `temporal`).
Default compose runs in **dev mode** (`PORTUNUS_DEV=1`). Edit `deploy/runner-policy.json` (from
`deploy/runner-policy.json.example`) to allow `npx`/`uvx` lookup dirs or absolute binaries.

```bash
PORTUNUS_URL=http://localhost:8080 PORTUNUS_OPERATOR_PASSWORD=... E2E_COMMAND=/abs/allowlisted/mcp-binary \
  ./scripts/e2e_stdio_omni.sh
```

## Using it

1. Open `http://localhost:8080`, complete account setup / log in.
2. **Apps:** connect a catalog or custom MCP server, store secrets in vault fields, **Probe**, then enable.
3. **Keys:** mint an agent key (shown once), grant it servers.
4. Point the agent at `http://HOST:8080/mcp` with `Authorization: Bearer pm-...`. The agent sees:
   `portunus_discover_servers`, `portunus_discover_tools`, `portunus_get_tool_schema`, `portunus_invoke_tool`.

### Agent / client notes

- Meta-tool payloads use **`input_schema`** (snake_case), not the MCP wire field `inputSchema`.
- Requests without a **`User-Agent`** may get `403` from Cloudflare (or similar) in front of a public deploy.
- Treat agent keys as secrets: prefer an env var or credential file over an inline Bearer header in
  synced configs; rotate if a config was backed up somewhere untrusted.
- When an upstream schema drifts, set a **schema override** in the tool drawer; probes keep the probed copy.
- Invoke retries once on `upstream_timeout` only for read-only / idempotent tools
  (`readOnlyHint` / `idempotentHint`, or known WebDAV list/stat/read tools).

## Environment variables

| Variable | Used by | Required | Meaning |
|---|---|---|---|
| `PORTUNUS_SECRETS_KEY` | gateway | yes | Vault keyring `k1:<base64 32 bytes>[,k2:...]`; newest version seals, older still open |
| `PORTUNUS_PEPPER_FILE` | gateway | yes | File holding base64 of >= 32 bytes; HMAC pepper for agent keys |
| `PORTUNUS_OPERATOR_PASSWORD` | gateway | until an account exists | Bootstrap password for `POST /api/auth/setup`; afterward login uses the account password |
| `PORTUNUS_TURNSTILE_SITEKEY` | gateway | no | Public Turnstile sitekey (`GET /api/auth/status`) |
| `PORTUNUS_TURNSTILE_SECRET` | gateway | no | When set, password login requires a passing Turnstile token |
| `PORTUNUS_TURNSTILE_HOSTNAMES` | gateway | with the secret | Comma-separated hostnames Siteverify must return (empty fails closed) |
| `PORTUNUS_TOKEN_FILE` | both | yes | Shared secret on every gateway→runner frame. `PORTUNUS_TOKEN` accepted in dev only |
| `PORTUNUS_SOCKET` | both | no | Runner Unix socket (default `/run/portunus/runner.sock`) |
| `PORTUNUS_SITE_ID`, `PORTUNUS_REGIONS` | runner | no | Fleet metadata on hello (default `local`) |
| `PORTUNUS_RUNNERS` | gateway | no | Fleet: `[site=]socket_or_url:token_file` list (see Fleet) |
| `PORTUNUS_POLICY` | both | no | `runner-policy.json`: `trusted_path`, `binary_allowlist` |
| `PORTUNUS_DATABASE_URL` | gateway | no | Default SQLite; Postgres: `postgresql+asyncpg://...` |
| `PORTUNUS_BIND` | gateway | no | `host:port` (default `127.0.0.1:8080`) |
| `PORTUNUS_DASHBOARD_DIR` | gateway | no | Built dashboard (default `dashboard/dist`) |
| `PORTUNUS_RUNNER_UID` / `PORTUNUS_GATEWAY_UID` | gateway / runner | prod | Peer uid pinning outside `PORTUNUS_DEV` |
| `PORTUNUS_DROP_PRIVS` | runner | prod | `1` = children as `portunus-mcp` after setuid / no_new_privs |
| `PORTUNUS_DEV` | both | no | Local evaluation: no privilege drop, no peer-uid pin, non-Secure cookie |
| `PORTUNUS_INSECURE_COOKIE` | gateway | no | Drop the `Secure` cookie flag (plain HTTP) |
| `PORTUNUS_TRUSTED_PROXIES` | gateway | no | CIDRs whose `X-Forwarded-For` is honoured for login throttling |
| `PORTUNUS_HEARTBEAT_S`, `PORTUNUS_GRACE_S` | both | no | Heartbeat (10s) / grace before reaping after link loss (30s) |
| `PORTUNUS_SKIP_MIGRATE` | gateway | no | `1` = skip `alembic upgrade head` on start |
| `TEMPORAL_HOST` | gateway / worker | no | When set, enables Temporal client + embedded probe worker + `portunus-worker` |
| `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE` / `TEMPORAL_PROBE_TASK_QUEUE` | worker | no | Defaults `default` / `portunus-control` / `portunus-probe` |
| `PORTUNUS_PROBE_INTERVAL_S` / `PORTUNUS_OAUTH_REFRESH_SKEW_S` | worker | no | Probe loop interval; refresh skew before `token_expires_at` |

## Production

Dev mode is not production. Run the gateway as `portunus`, the runner as `portunus-runner` with
`CAP_SETUID|CAP_SETGID|CAP_KILL`, children as `portunus-mcp`, leave `PORTUNUS_DEV` unset and set
`PORTUNUS_DROP_PRIVS=1`, keep a root-owned `0644` policy at `/etc/portunus/runner-policy.json`, and terminate TLS
in front of the gateway (session cookie is `Secure` outside dev). See design §3 and §7.

**`CAP_KILL` is required for production drop-privs.** Without it, grace/stop/kill of `portunus-mcp` children fail with `EPERM` and a new-epoch hello can hang. The runner drops everything but these three caps after binding; children keep none.

## Fleet (optional)

Several runners can be pooled: give each a `PORTUNUS_SITE_ID` / `PORTUNUS_REGIONS`, then list sockets (or TLS URLs) in `PORTUNUS_RUNNERS`.

A server's `preferred_regions` is a soft preference among *healthy* sites. On network-class failures
(`upstream_timeout`, `runner_unavailable`, `upstream_crash`) the gateway records per-(server, site) health,
stops placing new children on bad sites, and retries another site in the same probe when possible.
Success clears the mark; marks expire after 15 minutes. A child stays on the site that started it until then.

**Convention:** catalog seeds that need open egress (e.g. Yandex/Drime/Box/OneDrive rclone) default to
`preferred_regions=["overseas"]`. Tag the matching runners with `PORTUNUS_REGIONS=...,overseas` (or set
per-server regions yourself). No vendor or host names are hardcoded — your labels, your fleet.
`scripts/e2e_two_runners.sh` exercises local failover. Design: `docs/superpowers/specs/2026-11-09-portunus-fleet-design.md`.

### Remote runner links (TLS)

```bash
PORTUNUS_RUNNERS='cn=/run/portunus/runner.sock:/run/portunus/token,hk=tls://runner-hk.example.com:443:/etc/portunus/hk.token'
```

| Env | Meaning |
| --- | --- |
| `PORTUNUS_LISTEN=tls://0.0.0.0:443` | Runner listens on TCP+TLS |
| `PORTUNUS_TLS_CERT` / `PORTUNUS_TLS_KEY` | Runner server certificate |
| `PORTUNUS_TLS_CLIENT_CA` | Optional mTLS (require gateway client certs) |
| `PORTUNUS_RUNNER_CA` | CA that signs runner certs (gateway) |
| `PORTUNUS_RUNNER_TLS_CERT` / `PORTUNUS_RUNNER_TLS_KEY` | Optional gateway client cert |
| `PORTUNUS_RUNNER_TLS_INSECURE=1` | Dev only: skip verification |

`tcp://host:port` is loopback/dev only and refused off-loopback outside `PORTUNUS_DEV`.

## Temporal workflows (optional)

With `TEMPORAL_HOST` set (compose profile `temporal`):

| Workflow id | Purpose |
|---|---|
| `oauth-refresh` | Shared proactive OAuth access-token refresh (one workflow; per-server activities) |
| `server-probe` | Shared active-probe scheduler; `probe_server` on `portunus-probe` inside the gateway (sole-client pool); GFW-aware via preferred_regions + SiteHealth |
| `connect-ensure:{id}` | Connect → probe → enable → start children |
| `fleet-skills-sync:*` | Fleet skill-tree sync |

Details: `packages/portunus_worker/README.md`.

## Limitations

- Dev mode (and default compose) does not enforce the child privilege drop.
- Single gateway worker process; scale runners, not gateway replicas, for stdio sole-client correctness.
- Remote MCP OAuth is operator-driven code+PKCE with explicit authorize/token URLs (no Dynamic Client Registration / RFC 9728 discovery yet). Paste-token always works.
- Vault key rotation is manual (edit keyring + re-save server env); no idle-child auto-stop yet; audit rows are not auto-pruned.

## Tests

```bash
cd gateway && uv run pytest
cd runner && uv run pytest
cd packages/portunus_shared && uv run pytest
cd packages/portunus_worker && uv run pytest
```

## Security

See [SECURITY.md](SECURITY.md). Do not commit `.env`, vault keys, peppers, runner tokens, or agent keys.

## License

[MIT](LICENSE) © 2026 HUANG Cheng
