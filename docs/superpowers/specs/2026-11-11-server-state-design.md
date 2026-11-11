# Portunus — Server State in Discovery

**Date:** 2026-11-11  
**Status:** design  
**Covers:** assessment P0-2 (real server state) and P1-2 (tests that fields follow reality).  
**Related:** `2026-11-11-attribution-design.md`, `2026-11-11-fleet-logs-design.md`.

## 1. Problem

`portunus_discover_servers` returns a `status` for each server. An agent planning work across several servers reads it as "safe to use". Today it does not reflect what the gateway has observed: after a call to a server fails with `auth_required`, the next discovery still says `ok`.

## 2. What the code does today (checked 2026-11-11)

- `Omni._discover_servers` sets `status` to `ok`, with one exception: an OAuth remote server with no stored `Authorization` vault entry gets `auth_required`. So the field is not a pure constant, but it only looks at stored credentials.
- Nothing is written back from the call path. `_run_remote_invoke` returns an `auth_required` error with `data.action: "reauth"` and stores nothing. Stdio failures are the same.
- `tool_count` is the number of enabled tool rows, so a server that was never probed reports `0`, not "unknown". There are no timestamps.
- `Registry._probing` holds the servers with a probe in flight. `_commit_probe` writes tool rows and an audit event on success. A failed probe records nothing.
- `server_view` already exposes `has_auth` (stored `Authorization`) and `has_skill_session` (platform login completed for a skill-bridge app).
- Discovery lists only enabled servers. The dashboard enables a server after a successful probe.
- The dashboard shows enabled or paused and a tool count. It shows no health.

## 3. Decisions

1. **Four states:** `probing`, `ok`, `auth_required`, `unreachable`. The assessment also lists `degraded`. It is left out because it has no definition that a test could check. It can be added later with one, for example an error rate over a window.
2. **Status is derived when read**, from stored observations plus live facts. No status value is stored, so it cannot drift from the facts behind it.
3. **No background prober.** State reflects the last observation and carries timestamps so its age is visible. A credential change triggers a probe (§6).
4. **One vocabulary.** Discovery and the operator API return the same fields, so the dashboard and agents see the same state.

## 4. Stored observations

New nullable columns on `servers` (migration `0009`):

| Column | Meaning |
|---|---|
| `last_probe_at` | Last successful probe |
| `last_ok_at` | Last successful call or probe |
| `last_error_code` | Code of the last failure that counts (§5) |
| `last_error_at` | When that failure happened |

## 5. Which outcomes count

| Outcome | Effect |
|---|---|
| Call or probe succeeded | Sets `last_ok_at` (and `last_probe_at` for a probe) |
| `auth_required` | Sets `last_error_*` |
| `runner_unavailable`, `spawn_rejected`, `spawn_failed`, `upstream_crash` from the start, rpc or remote path | Sets `last_error_*` |
| `upstream_timeout` at the gateway's default timeout | Sets `last_error_*` |
| `upstream_timeout` when the caller shortened `timeout_s` | None. A 1 s budget says nothing about the server |
| `upstream_error` (the upstream answered with a tool-level error) | None. The server responded; the gateway cannot interpret the error text |
| Caller or policy rejections: `invalid_tool_arguments`, `schema_drift`, `tool_missing`, `tool_disabled`, `not_granted`, `rate_limited`, `child_cap`, `cancelled`, `payload_too_large`, and `runner_unavailable` raised because a previous child is still resolving | None |

To limit writes, a success updates `last_ok_at` only when the server is not currently `ok` or the stored value is more than 60 s old. Failures are always written.

## 6. Deriving status

The first matching rule wins:

1. A probe is in flight, or the server has never been probed and has no recorded failure → `probing`.
2. Credentials are missing: an OAuth remote without a stored `Authorization`, or a platform-login skill-bridge app without `SKILL_SESSION` → `auth_required`.
3. The server is stdio and no runner is connected → `unreachable`.
4. `last_error_at` is newer than `last_ok_at` → `auth_required` if the code is `auth_required`, otherwise `unreachable`.
5. Otherwise → `ok`.

`tool_count` is `null` until the first successful probe, then the number of enabled tools.

Recovery:

- A successful call or probe returns the server to `ok`.
- When credentials change (OAuth callback, auth token set, platform login success, vault update), the gateway clears `last_error_*` and runs a probe. The probe result sets the state. This also means a server gets its tools right after Connect, without a separate Probe click.
- A runner reconnecting clears rule 3 immediately, because that rule is evaluated live.
- An `unreachable` recorded from a failed call stays until the next attempt. `last_error_at` shows how old it is, and the tool description tells agents that a retry is allowed.

## 7. Discovery contract

```json
{
  "slug": "dropbox",
  "name": "Dropbox",
  "kind": "remote",
  "status": "auth_required",
  "tool_count": 24,
  "last_probe_at": "2026-11-10T08:14:02Z",
  "last_ok_at": "2026-11-11T03:40:11Z",
  "last_error_code": "auth_required",
  "last_error_at": "2026-11-11T09:02:47Z"
}
```

The operator API's server view gains the same six fields.

Agents rarely see `probing`, because discovery lists only enabled servers and a server is normally enabled after its first successful probe. The operator API shows it for every server.

## 8. Changes by component

| Component | Change |
|---|---|
| `db/models.py`, alembic `0009` | Four columns on `servers` |
| `registry.py` | `record_outcome(server_id, code)`; probe success and failure both recorded; credential writes clear the error and start a probe; `server_view` gains the fields |
| `omni.py` | `_invoke` reports each outcome; `_discover_servers` uses the shared derivation; tool description updated |
| New `health.py` | The derivation function, used by discovery and the operator API |
| `dashboard` | Status chip on Apps cards from `status`; the "Paused" badge stays tied to `enabled` |

## 9. Tests

Each test changes reality and asserts that the field follows. They are parameterised over server kind (stdio, remote, OAuth remote, platform-login skill-bridge) rather than written per server.

| Forced condition | Expected |
|---|---|
| New server, never probed | `probing`, `tool_count: null` |
| Probe in flight | `probing` |
| First probe fails | `unreachable`, `tool_count: null`, `last_error_code` set |
| Probe succeeds | `ok`, `tool_count` set, `last_probe_at` set |
| A call returns `auth_required` | Discovery within 5 s reports `auth_required` |
| Reconnect after that | Probe runs; `ok` if it succeeds |
| A call crashes or times out at the default timeout | `unreachable` |
| A call times out with a caller-shortened `timeout_s` | State unchanged |
| A call returns `upstream_error` or an argument error | State unchanged |
| Last runner disconnects (stdio server) | `unreachable`; back to the previous state on reconnect |
| OAuth remote with the `Authorization` entry removed | `auth_required` |

## 10. Deferred

- `degraded`, once it has a testable definition.
- Background re-probing of servers that are not `ok`, with backoff. Worth adding if stale `unreachable` states turn out to mislead agents.
