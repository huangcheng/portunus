# Portunus

Self-hosted MCP gateway: register `npx`/`uvx` stdio MCP servers, keep their secrets in an encrypted
vault, hand agents scoped keys, and expose everything through one Streamable HTTP endpoint
(`/mcp`, four `portunus_*` meta tools). Design: `docs/superpowers/specs/2026-11-09-portunus-design.md`.

| Piece | What it is |
|---|---|
| `gateway/` | FastAPI app: operator API (`/api`), dashboard (`/`), Omni (`/mcp`), vault, keys/grants, slots |
| `runner/` | Unix-socket listener that spawns/reaps stdio children for the gateway (sole client) |
| `packages/portunus_shared/` | argv grammar, wire codec, error codes shared by both |
| `dashboard/` | React operator UI (built to `dashboard/dist`, served by the gateway) |

## Quickstart (local, dev mode)

Needs `uv`, `python3`, `curl`. Dev mode (`PORTUNUS_DEV=1`) skips the privilege drop and uid checks;
**never use it for anything but local evaluation.**

```bash
# 1. Prove the whole path works (starts a throwaway runner + gateway, runs the scenario, cleans up)
./scripts/e2e_stdio_omni.sh
```

The script: logs in as operator, mints agent keys, registers a fixture stdio MCP server, probes it,
enables a tool + the server, grants the key, then discovers and invokes the tool through `/mcp`.
It also checks that ungranted keys, disabled tools, bad keys and revoked keys are refused, and exits
non-zero on any surprise (logs of a failed run are kept in the printed temp dir).

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
uv run python -m portunus_gateway # gateway on http://127.0.0.1:8080 (migrates the DB on start)
```

Keep `PORTUNUS_SECRETS_KEY` and the pepper file: losing the key makes vaulted env unreadable, and
changing the pepper invalidates every agent key.

## Quickstart (docker compose)

```bash
./scripts/init_env.sh            # writes .env + secrets/{pepper,runner.token}; prints the operator password
docker compose up --build        # gateway + runner on http://localhost:8080
# optional Postgres instead of SQLite:
#   set WITH_POSTGRES=1 and PORTUNUS_DATABASE_URL=postgresql+asyncpg://portunus:portunus@postgres:5432/portunus
#   in .env, then: docker compose --profile postgres up --build
```

Verified: `docker compose build` and `up` succeed; gateway reports `/healthz` ok with the runner connected (the e2e script itself was not run against containers). The gateway is published on **127.0.0.1 only** (`PORTUNUS_PORT` changes the port, not the interface); to expose
it, put a TLS reverse proxy in front rather than widening the bind in dev mode.

**Secret file modes.** `secrets/` is `0700` and `.env` is `0600`. `secrets/pepper` and `secrets/runner.token` are
`0644` because compose bind-mounts keep host modes and the container user (uid 10001) must read them; host
users other than you cannot reach them through the `0700` directory, but anything that can read the file (root, a
process of yours, a backup of the dir) can. For stricter modes, `chmod 0600` them and run the services as your
own uid (`user:` in `docker-compose.yml`) with `run`/`data` volumes owned by that uid, or use Docker/Swarm secrets.

Services: `portunus` (gateway, dashboard, `/mcp`), `portunus-runner` (no published port; shares only the
socket volume), `postgres` (profile `postgres`). The default compose stack runs in **dev mode**
(`PORTUNUS_DEV=1`, same uid for gateway and runner). Edit `deploy/runner-policy.json` (copied from
`deploy/runner-policy.json.example`) to allow `npx`/`uvx` lookup dirs or absolute binaries.

To run the e2e script against a compose stack, the stdio server must be an absolute, allowlisted
binary inside the runner container:

```bash
PORTUNUS_URL=http://localhost:8080 PORTUNUS_OPERATOR_PASSWORD=... E2E_COMMAND=/abs/allowlisted/mcp-binary \
  ./scripts/e2e_stdio_omni.sh   # the default fixture assertions apply to the bundled fixture only
```

## Using it

1. Open `http://localhost:8080`, log in with the operator password.
2. **Servers:** register an stdio or remote server, put secrets in `env` / vault headers (encrypted, never
   returned), **Probe** it, then enable the server. Probed tools are available immediately.
3. **Keys:** mint an agent key (shown once), grant it servers.
4. Point the agent at `http://HOST:8080/mcp` with `Authorization: Bearer pm-...`. The agent sees four
   tools: `portunus_discover_servers`, `portunus_discover_tools`, `portunus_get_tool_schema`,
   `portunus_invoke_tool`.

### Agent / client notes

- Meta-tool payloads use **`input_schema`** (snake_case), not the MCP wire field `inputSchema`.
- Requests without a **`User-Agent`** may get `403` from Cloudflare (or similar) in front of a public deploy.
- Treat agent keys as secrets: prefer an env var or credential file over an inline Bearer header in
  synced configs; rotate the key if a config file was backed up to an untrusted place.
- When an upstream schema drifts (optional vs required), open the server’s tool drawer and set a
  **schema override**; probes keep the probed copy but agents validate against the override.
- Invoke retries once on `upstream_timeout` only for read-only / idempotent tools
  (`readOnlyHint` / `idempotentHint`, or known WebDAV list/stat/read tools).

### Ops checklist (probe findings)

| Issue | Action |
|---|---|
| Dropbox / PikPak `auth_required` | Reconnect OAuth in Apps → Probe → confirm status `ok` |
| Guangyapan `upstream_error` / SSL | On the runner: `SKILL_ROOT` / `STATE_DIR`, Node deps, device login, reachability of `api.guangyapan.com` |
| Quark `-104` | Expected: Quark rejects this runtime; leave out of automation (catalog marks it non-popular) |
| dida365 `query_calendar_events` | Set an operator schema override that marks `query` required |

## Environment variables

| Variable | Used by | Required | Meaning |
|---|---|---|---|
| `PORTUNUS_SECRETS_KEY` | gateway | yes | Vault keyring `k1:<base64 32 bytes>[,k2:...]`; newest version seals, older still open |
| `PORTUNUS_PEPPER_FILE` | gateway | yes | File holding base64 of >= 32 bytes; HMAC pepper for agent keys |
| `PORTUNUS_OPERATOR_PASSWORD` | gateway | until an account exists | Legacy operator password (argon2id hash in memory). Optional at boot. `POST /api/auth/setup` requires it as `bootstrap_password` when set; after the account exists, login uses the account password, not this value |
| `PORTUNUS_TURNSTILE_SITEKEY` | gateway | no | Public Turnstile sitekey, returned by `GET /api/auth/status` |
| `PORTUNUS_TURNSTILE_SECRET` | gateway | no | Turnstile secret. When set, password login requires a passing `action=login` siteverify |
| `PORTUNUS_TURNSTILE_HOSTNAMES` | gateway | with the secret | Comma-separated hostnames Siteverify must return. Empty allowlist fails closed |
| `PORTUNUS_TOKEN_FILE` | both | yes | Shared secret sent on every gateway->runner frame. `PORTUNUS_TOKEN` accepted in dev only |
| `PORTUNUS_SOCKET` | both | no | Runner Unix socket (default `/run/portunus/runner.sock`) |
| `PORTUNUS_SITE_ID`, `PORTUNUS_REGIONS` | runner | no | Fleet metadata advertised on hello: a site name (default `local`) and comma-separated region labels (default `local`) |
| `PORTUNUS_RUNNERS` | gateway | no | Fleet: comma-separated `[site=]socket_path:token_file` links, replacing `PORTUNUS_SOCKET`/`PORTUNUS_TOKEN_FILE` (see Fleet) |
| `PORTUNUS_POLICY` | both | no | `runner-policy.json` (default `/etc/portunus/runner-policy.json`): `trusted_path`, `binary_allowlist`. The gateway pre-validates with it; the runner re-validates before every exec |
| `PORTUNUS_DATABASE_URL` | gateway | no | Default `sqlite+aiosqlite:///./data/portunus.db`; Postgres: `postgresql+asyncpg://...` |
| `PORTUNUS_BIND` | gateway | no | `host:port` (default `127.0.0.1:8080`) |
| `PORTUNUS_DASHBOARD_DIR` | gateway | no | Built dashboard (default `dashboard/dist`; API-only if absent) |
| `PORTUNUS_RUNNER_UID` | gateway | prod | Uid the runner socket peer must have (default: uid of user `portunus-runner`) |
| `PORTUNUS_GATEWAY_UID` | runner | prod | Uid the gateway peer must have (default: uid of user `portunus`) |
| `PORTUNUS_DROP_PRIVS` | runner | prod | `1` = children run as `portunus-mcp` after setgroups/setgid/setuid/capset/no_new_privs |
| `PORTUNUS_DEV` | both | no | `1` = local evaluation: no privilege drop, no peer-uid pinning, non-Secure session cookie |
| `PORTUNUS_INSECURE_COOKIE` | gateway | no | `1` = drop the `Secure` cookie flag (plain-http setups) |
| `PORTUNUS_TRUSTED_PROXIES` | gateway | no | Comma-separated CIDRs whose `X-Forwarded-For` is honoured for login throttling |
| `PORTUNUS_HEARTBEAT_S`, `PORTUNUS_GRACE_S` | runner/gateway | no | Heartbeat interval (10s) / runner grace before reaping children after losing the gateway (30s) |
| `PORTUNUS_SKIP_MIGRATE` | gateway | no | `1` = do not run `alembic upgrade head` on start |
| `PORTUNUS_REMOTE_LOOPBACK_ALLOWLIST` | gateway | no | Loopback hosts remote MCP servers may target (see Limitations) |

## Production

Dev mode is not production. Run the gateway as `portunus`, the runner as `portunus-runner` with
`CAP_SETUID|CAP_SETGID|CAP_KILL`, children as `portunus-mcp`, set `PORTUNUS_DEV` unset and
`PORTUNUS_DROP_PRIVS=1`, a root-owned `0644` policy at `/etc/portunus/runner-policy.json`, and terminate TLS
in front of the gateway (the session cookie is `Secure` outside dev). See design §3 and §7.

**`CAP_KILL` is required for production drop-privs.** Children run as `portunus-mcp`, a different uid
from the runner, so the runner cannot signal them without `CAP_KILL`: grace, stop and kill fail with
`EPERM`, orphaned children are not reaped, and a new-epoch hello can hang waiting for the previous epoch's
children. `PORTUNUS_DROP_PRIVS=1` without `CAP_KILL` is **unsupported**. The design's two-cap residual
(§3) is therefore three caps in practice; the runner drops everything but these three after binding, and
the children keep none.

## Limitations

- Dev mode (and the default compose stack) does not enforce the child privilege drop.
- Single gateway worker. Multiple runners are optional and local-socket only (see Fleet below); there is no
  native remote/TLS runner transport yet (SSH tunnels are an ops choice).
- Remote MCP OAuth is operator-driven code+PKCE with explicit authorize/token URLs (no Dynamic Client
  Registration / protected-resource metadata discovery yet). Paste-token always works.

Known follow-ups (documented gaps):

- Vault key rotation endpoint (re-seal under the newest key version); rotate by editing `PORTUNUS_SECRETS_KEY`
  keyring and re-saving each server's env manually.
- Idle child stop (the design's 15 min idle timeout): stdio children stay up until revoked, ungranted,
  disabled, or the runner/gateway restarts.
- Cancel-operation API/tool for in-flight invocations.
- 30-day audit log retention/pruning: audit rows are kept indefinitely.
- Remote (TLS) runner links and automatic re-homing of children off a dead site.
- MCP OAuth discovery (RFC 9728) and refresh-token rotation.

## Fleet (optional)

By default one gateway talks to one runner. Several runners can be pooled on one host: give each runner a
`PORTUNUS_SITE_ID` and `PORTUNUS_REGIONS` (placeholders such as `site-a` / `region-a`), then list their
sockets and token files in the gateway's `PORTUNUS_RUNNERS`
(`site-a=/run/a.sock:/path/a.token,site-b=/run/b.sock:/path/b.token`; the `site=` prefix is optional).

A server's `preferred_regions` is a soft preference honored only among *healthy* sites — it degrades
to a hint, never a pin into a partition. On top of it the gateway learns per-(server, site) network
health: consecutive network-class failures (`upstream_timeout`, `spawn_failed`) on one runner site
mark that (server, site) pair, the scheduler stops placing new children there (they cold-start on
another site), and the stuck pair is retired at the threshold. Any success clears the mark, and marks
expire after 15 minutes — a GFW-shaped partition fails over without operator action. A child stays on
the site that started it until then, so a dead link surfaces as `runner_unavailable`, not silent moves.
`scripts/e2e_two_runners.sh` exercises this locally (two runners, kill one, check fallback); it uses only
temp sockets/secrets under `/tmp`. Design: `docs/superpowers/specs/2026-11-09-portunus-fleet-design.md`.

### Remote runner links (TLS)

Runners on other hosts — or other countries — listen on TCP with TLS instead of a Unix socket. The link
carries the same frame protocol; TLS plus the runner token (and optionally a client certificate) replaces
the SO_PEERCRED uid gate. This is the intended shape for geo-split fleets: runners needing overseas egress
(Dropbox, Google) live outside the restricted network, China-service runners stay inside, and
`preferred_regions` routes each server's children accordingly.

Runner side:

| Env | Meaning |
| --- | --- |
| `PORTUNUS_LISTEN=tls://0.0.0.0:443` | listen on TCP+TLS instead of the Unix socket |
| `PORTUNUS_TLS_CERT` / `PORTUNUS_TLS_KEY` | server certificate (required for `tls://`) |
| `PORTUNUS_TLS_CLIENT_CA` | optional: require gateway client certificates (mTLS) |

Gateway side — list the link in `PORTUNUS_RUNNERS` with the URL form (token file after the last colon):

```bash
PORTUNUS_RUNNERS='cn=/run/portunus/runner.sock:/run/portunus/token,hk=tls://runner-hk.example.com:443:/etc/portunus/hk.token'
```

| Env | Meaning |
| --- | --- |
| `PORTUNUS_RUNNER_CA` | CA bundle that signs the runner's certificate (pin a private CA; default: system roots) |
| `PORTUNUS_RUNNER_TLS_CERT` / `PORTUNUS_RUNNER_TLS_KEY` | optional client certificate for mTLS runners |
| `PORTUNUS_RUNNER_TLS_INSECURE=1` | dev only: skip certificate verification |

`tcp://host:port` (cleartext) exists for loopback/dev only and is refused off-loopback outside dev, per the
fleet design. Ops notes for links crossing restrictive networks: terminate on :443 with a clean IP and an
ordinary-looking certificate, keep one connection per site (the runner accepts a single client anyway),
and raise `PORTUNUS_HEARTBEAT_S` on both ends if the path is lossy — reconnection reconciles slot state
from the runner's inventory, so a dropped link never strands children. An all-remote fleet needs no local
`portunus-runner` user (the uid gate applies to Unix links only).

## Tests

```bash
cd gateway && uv run pytest        # gateway (includes real runner + fixture MCP server)
cd runner && uv run pytest
cd packages/portunus_shared && uv run pytest
```
