# Portunus — MCP Gateway Design

**Date:** 2026-11-09  
**Status:** draft (pi panel passed — 4/4 GO on round 15; audits under `/tmp/portunus-audit/`)  
**Sibling:** Janus — LLM front door. Portunus is the MCP front door and central tool vault.

**One-liner:** Self-hosted MCP gateway: register `npx`/`uvx` and remote MCP servers, operator vault, agent keys with grants, Omni `/mcp` (MCP Streamable HTTP).

## 1. Problem

Many agents × many MCP servers. Composio (2026-11) lacks stdio CLI registration. Uber’s MCP Gateway post motivates registry + proxy + Omni discovery.

## 2. Goals / non-goals

**Goals:** vault; agent keys + grants; `stdio` + `remote`; gateway/runner; Omni `/mcp`; disabled-until-enabled; dashboard; proxy third-party MCP.

**Non-goals:** API-as-tools DSL; per-caller OAuth; per-tool grants; quotas/billing; flatten upstream tools; containers as primary isolation; shared DB with Janus; child-initiated MCP; full transitive lockfiles; filesystem isolation between same-uid children (residual — see §7).

## 3. Architecture

One gateway worker + one runner.

**Topology:** **Runner listens** on the Unix socket; **gateway connects** as the sole client. Runner rejects a second concurrent connection.

| | Role |
|---|---|
| Gateway | Registry, vault, keys/grants, `/mcp`, remote proxy, caps |
| Runner | Listen/accept; spawn/reap/bridge |

**OS users:** `portunus` (gateway ∈ `portunus-ipc`), `portunus-runner` (runner ∈ `portunus-ipc`), `portunus-mcp` (children; not in `portunus-ipc`).

**Runner privilege:** install grants the runner `CAP_SETUID|CAP_SETGID` (residual: root-equivalent for process identity — accepted). After binding as `portunus-runner`, retain only those caps for spawn. Child spawn sequence: close surplus fds → `setgroups([])` → `setgid(portunus-mcp)` → `setuid(portunus-mcp)` → **clear permitted/effective/inheritable/ambient capability sets (empty `capset`)** → `prctl(PR_SET_NO_NEW_PRIVS)` → `exec`. Caps must not survive into the child.

**Paths:** `/var/lib/portunus` `0700` portunus. `/run/portunus` `0770` `portunus-runner:portunus-ipc`. Token `0640` same group. Policy `/etc/portunus/runner-policy.json` mode `0644` root-owned (readable by runner) — contents: `trusted_path`, `binary_allowlist` (realpaths). Parent cache `/var/cache/portunus` mode `0755` uid `portunus-mcp`; per-server subdirs created by a `portunus-mcp` helper via `mkdir -p`. Sibling cache reads remain an accepted same-uid residual.

**Connect auth:** runner checks peer uid=`portunus` (+ pid). Gateway checks peer uid=`portunus-runner`. Token on every frame. `gateway_epoch` (UUID minted once per gateway process lifetime) + `conn_id` on every gateway→runner request; runner drops superseded epoch/conn.

**Authz:** in-process on gateway; rebuilt from DB on start.

## 4. Agent keys

- `pm-<key_id>.<secret>`; verify every `/mcp` request: look up `key_id`, compute `HMAC-SHA256(key=pepper, msg=secret)`, constant-time compare to stored digest (bearer secret is the credential; no separate replay counter).
- Pepper rotation = mass reissue.
- Step-up as before (includes enable/disable, ungrant, allowlist, …).
- **Revoke key:** bump generation; fail `/mcp`; mark **all** that key’s children `pending_stop`; stop/kill when reachable; cancel RPCs.
- **Ungrant server:** update grants; `pending_stop` that pair; stop/kill when reachable; cancel RPCs.
- Reachable: death ≤5s. Unreachable: enforce on reconnect or runner grace expiry. Routing cut ≤1s always.
- One key per agent instance recommended.

## 5. Omni `/mcp`

Streamable HTTP; four `portunus_*` tools; cursor pages 50/100; body/result ≤1MiB → `payload_too_large`; 60/min/key; 8 concurrent invokes/key.

**Probe:** dashboard/scheduler only; pool namespace `probe`; max 2; outside invoke singleton/caps.

## 6. Stdio

Literals only on the wire. **Gateway validates on write/start; runner re-validates the identical grammar + policy.json immediately before `exec` (runner authoritative at spawn).**

**Closed argv grammar (normative):**

- `command` is basename `{npx,uvx}` resolved via policy `trusted_path`, or absolute path whose `realpath` ∈ policy `binary_allowlist`.
- `npx` argv = optional `{-y|--yes}` + required `--ignore-scripts` + exactly one package token matching `name@MAJOR.MINOR.PATCH` or `@scope/name@MAJOR.MINOR.PATCH` (digits only in each semver field; no `latest`, dist-tags, ranges, `git+`, `file:`, URLs) + zero or more trailing args that do not start with `-` unless each is exactly listed in the start-frame `trailing_allowlist` (default empty).
- `uvx` argv = exactly one `name==MAJOR.MINOR.PATCH` + trailing rule as above; child env includes `UV_NO_BUILD=1`.
- Absolute allowlisted binary: argv = only `trailing_allowlist` entries (default empty — typically argv `[]`).
- Any other shape → `spawn_rejected` at gateway and at runner.

### 6.1 Slots

- One invoke child per `(server_id, agent key_id)`; mutex; caps 32/key, 256 global.
- Gateway-local FSM: reserve → start (`child_id` from gateway) → commit. Rollback frees reserved. Committed frees only on exit evidence (`child_exited` or inventory absence). **No force-free without exit evidence.**
- Start ack timeout 15s → `kill` if socket up. If runner reports unknown/`ESRCH` (never started), **rollback** reserve immediately. Otherwise mark `pending_stop` on that `child_id` (survives reconnect) and retry kill until `child_exited` or disconnect; slot stays reserved until exit evidence or inventory.
- Each child record stores `vault_version`; admission re-checks authz.
- **Server disable:** same stop path as ungrant for that `server_id` (all keys): `pending_stop` + cancel RPCs + kill when reachable.

**Grace / reconnect (normative):**

1. Gateway → runner `heartbeat` every 10s.  
2. If runner sees heartbeat gap >30s **or** read EOF: runner **closes the socket** (if still open) and starts a **local** 30s grace timer. Gateway observes EOF, fails RPCs (`runner_unavailable`/`cancelled`), and **retries connect until accepted** (sole-client: wait for runner to finish closing the old conn).  
3. Same-epoch reconnect **before** grace expiry **disarms** the grace timer, then `inventory` + reconcile. Inventory must list **every** live child the runner still has (complete set). Keep/adopt a listed child only if: key not revoked, server granted+enabled, not `pending_stop`, `vault_version` matches. Otherwise stop/kill. Absent ids → free slots. Adopt reserved→committed when listed+allowed (never double-spawn). Timed-out starts still `pending_stop` are not adopted.  
4. Grace expiry (no reconnect): runner SIGKILLs all children, **`waitpid`/reaps until none remain**, then may report empty inventory.  
5. New-epoch hello: same — SIGKILL, reap-to-empty, then inventory empty. Never advertise empty **or partial** inventory while unreaped children exist.

**Runner death:** supervisor **kills** then reaps `portunus-mcp` descendants; service manager restarts runner; gateway `runner_unavailable`; new `run_id` → empty inventory.

Idle 15m → stop. Vault bump → bump version + stop+start that server’s children; in-flight `cancelled`.

### 6.2 Wire

`hello`, `inventory`, `start`, `stop`, `kill`, `probe`, `rpc`, `cancel`, `heartbeat`; async `child_exited`. BE u32+JSON; 5MiB.

### 6.3 Remote

Default-deny non-public IPs except loopback allowlist; DNS pin per redirect hop; strip creds cross-origin; no https→http; TLS verify; non-loopback https. OAuth code+PKCE; paste token; `auth_required`.

### 6.4 Errors

`unauthorized`, `revoked`, `not_granted`, `server_disabled`, `tool_disabled`, `tool_missing`, `schema_mismatch`, `payload_too_large`, `upstream_timeout`, `upstream_crash`, `cancelled`, `auth_required`, `child_cap`, `rate_limited`, `runner_unavailable`, `spawn_rejected`, `spawn_failed`, `unsupported`.

## 7. Vault / trust

AES-256-GCM; keyring in data dir. Children cannot join IPC. Same-uid MCP children: mutual environ **and** cache access accepted residual. Caps + setuid drop are the hard boundary vs the gateway/runner.

## 8. Dashboard

Opaque sessions; Secure httpOnly SameSite; CSRF; argon2id; lockout 5/15min/IP + trusted_proxies; audit 30d metadata.

## 9. Stack

Python 3.12+ FastAPI (1 worker), Python runner, MCP SDK, React, Postgres 16 (SQLite OK). `portunus/{gateway,runner,dashboard,docs}`.

## 10. Invariants

1. Runner listens; gateway is sole client; children cannot auth to socket.  
2. Runner-owned grace: close socket → timer → kill+reap or disarm on same-epoch reconnect.  
3. Reconcile checks revoke, grants, pending_stop, vault_version; disable uses pending_stop.  
4. No empty inventory before reap; no force-free without exit evidence; ESRCH → rollback.  
5. Dual argv validation (gateway + runner); CAP_SETUID/SETGID residual named; `no_new_privs` on children.

## 11. Success criteria

Pinned Omni path; remote path; vault rotate; revoke/ungrant; child cannot read token; tests: setgroups drop, heartbeat-gap closes socket + reconnect disarm, grace expiry kill, reconcile drops revoked, vault_version mismatch kill, no force-free double-spawn, pin reject, child_cap.

## 12. Defaults

Invoke 60s; probe 60s; start ack 15s; grace 30s; heartbeat 10s.
