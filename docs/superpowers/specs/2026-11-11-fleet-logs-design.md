# Portunus — Fleet and Logs Pages

**Date:** 2026-11-11  
**Status:** design  
**Covers:** operator pages for runners and for event history.  
**Related:** `2026-11-11-attribution-design.md`, `2026-11-11-server-state-design.md`, `2026-11-09-portunus-fleet-design.md` (phase D).

## 1. Problem

The gateway runs against several runners, but the dashboard shows only "N runners live" in the sidebar. The operator cannot see which sites are connected, what is running on them, or whether a runner is on older code than the gateway. Audit events are written but cannot be read anywhere, and tool calls leave no record at all.

## 2. What the code does today (checked 2026-11-11)

- `GET /api/status` returns `ok`, `runner_connected` and `runners_connected`. Nothing else about the fleet is exposed.
- `RunnerPool` entries come from `PORTUNUS_RUNNERS` (`[site=]socket_path:token_file`) or the single default socket. An entry stays in the pool while disconnected.
- Per runner link the gateway knows `site_id`, `regions`, `egress_class`, `capacity` (from the hello reply), `connected`, `conn_seq` and `run_id`. It does not record when the link connected, when the last heartbeat succeeded, or what code the runner is running.
- `SlotManager` knows, per child: `child_id`, `server_id`, `key_id`, `state`, `site_id`, `preferred_miss`, `pending_stop`.
- `audit_events` is written by about twenty actions (server, tool, key, grant, catalog, operator login, `skill.auth`, `server.probe`). There is no read endpoint. The 30-day retention in the original design is not implemented.
- `skill.auth` writes an audit row for every action, including each status poll during a Connect login (one every 2 s).
- The dashboard has two views, switched by a state variable in `App.tsx`. There is no router.
- Production runs one gateway process on Postgres with three runners. The assessment's finding about two diverging SQLite registries does not match the current gateway. Only the gateway's configured backend was checked, not whether an older database still exists elsewhere.

## 3. Decisions

1. **One Fleet page**, covering the gateway, runners and running children. It is read-only in this version.
2. **One Logs page with two tabs:** Audit and Tool calls.
3. **Tool calls are stored in the gateway's database**, not read back from a tracing backend, so the page works with export off.
4. **Delivery is staged by what each part depends on:**

| Part | Depends on |
|---|---|
| Fleet page (runners, children, code drift) | nothing |
| Logs: Audit tab, retention | nothing |
| Logs: Tool calls tab | attribution spec |
| Server status chips on Fleet | server-state spec |

## 4. Fleet

### 4.1 Runner additions

- `RunnerClient` records `connected_since`, `last_heartbeat_at` and `last_disconnected_at`.
- The runner's hello reply gains `fingerprints`: one source fingerprint each for `portunus_runner`, `portunus_shared` and `portunus_skill_bridge`. The fingerprint is defined in the attribution spec. The field is additive; an old runner omits it.

The gateway compares the fingerprints of the two packages it shares with runners (`portunus_shared`, `portunus_skill_bridge`). A mismatch is reported as drift. This is the failure seen on 2026-11-11, when runners with an older `portunus_skill_bridge` rejected a new manifest and probes crashed.

### 4.2 API

`GET /api/fleet` (operator session required):

```json
{
  "gateway": {
    "version": "0.1.0+3fa9c1d2ab77",
    "started_at": "2026-11-11T08:59:41Z",
    "db_backend": "postgresql",
    "otel_export": false,
    "fingerprints": { "portunus_shared": "…", "portunus_skill_bridge": "…" }
  },
  "runners": [
    {
      "site_id": "site-a",
      "regions": ["region-east"],
      "egress_class": null,
      "capacity": null,
      "connected": true,
      "connected_since": "2026-11-11T09:00:12Z",
      "last_heartbeat_at": "2026-11-11T09:31:02Z",
      "last_disconnected_at": null,
      "reconnects": 3,
      "children": 2,
      "fingerprints": { "portunus_runner": "…", "portunus_shared": "…", "portunus_skill_bridge": "…" },
      "drift": ["portunus_skill_bridge"]
    }
  ],
  "children": [
    {
      "child_id": "…",
      "server": { "id": "…", "slug": "quark" },
      "key_id": "…",
      "site_id": "site-a",
      "state": "committed",
      "preferred_miss": false,
      "pending_stop": false
    }
  ]
}
```

`fingerprints` and `drift` are `null` for a runner that does not report them. `GET /api/status` and the public `/healthz` are unchanged; `/healthz` still exposes no fleet detail.

### 4.3 Page

- **Gateway strip:** version, start time, database backend, whether trace export is configured.
- **Runners table:** site, regions, connected or disconnected with the relevant timestamp, last heartbeat, reconnect count, child count, and a drift warning naming the packages that differ.
- **Children table:** server, key, site, state, and flags for a preference miss or a pending stop.

The page polls every 15 s, the same interval the sidebar uses. The sidebar's runner count becomes a link to it. There are no charts.

### 4.4 Acceptance

1. With no runner connected, every row shows disconnected and nothing shows healthy.
2. Killing a runner turns its row to disconnected within the heartbeat timeout plus one poll. Reading `runner_client.py`, that is about 30 s at the default 10 s heartbeat.
3. A runner whose `portunus_skill_bridge` differs from the gateway's shows drift for that package.
4. The children table matches `SlotManager` after a start, a stop and a child exit.
5. `/healthz` output is unchanged.

## 5. Logs: Audit

### 5.1 API

`GET /api/audit?limit=50&before=<cursor>&action=<prefix>&target=<exact>` returns rows newest first, as `{id, created_at, actor, action, target, detail}`, with a `next` cursor. Paging is by `(created_at, id)`.

### 5.2 Changes

- Add an index on `audit_events.created_at`.
- `skill.auth` stops writing a row for status polls that are still pending. It keeps rows for login start, the final success or failure, and logout.
- A periodic task in the gateway lifespan deletes rows older than `PORTUNUS_AUDIT_RETENTION_DAYS` (default 30, the figure in the original design).

Audit details are metadata by design. The `skill.auth` detail was read and holds booleans and names only. The other actions were not individually reviewed, so the tests below cover them.

### 5.3 Acceptance

1. Minting a key, adding a grant and connecting an app each produce a row visible through the API.
2. A complete Connect login produces a bounded number of `skill.auth` rows, not one per poll.
3. For every audited action, a test runs it with a sentinel secret and asserts the string appears in no audit detail.
4. Rows older than the retention period are deleted.

## 6. Logs: Tool calls

### 6.1 Storage

New table `tool_calls`:

| Column | Notes |
|---|---|
| `id`, `created_at` | Indexed on `created_at` |
| `trace_id` | Indexed |
| `key_id`, `server_slug`, `tool` | |
| `outcome` | `ok` or the error code |
| `site_id`, `cold_start`, `preferred_miss` | Null for remote servers |
| `gateway_ms`, `start_ms`, `queue_ms`, `init_ms`, `upstream_ms` | From the attribution spec |
| `result_bytes` | Size only |

One row is written per `portunus_invoke_tool` call that passed key authentication, whatever the outcome. `rate_limited` rejections are not stored; a runaway caller would otherwise fill the table. They are counted in metrics. Discovery calls are not stored.

Arguments and results are never stored.

A failed insert is logged and does not affect the call's result. Rows older than `PORTUNUS_TOOL_CALL_RETENTION_DAYS` (default 14) are deleted by the same periodic task.

### 6.2 API

`GET /api/tool-calls?limit=50&before=<cursor>&server=&outcome=&key_id=&site_id=&trace_id=`, newest first.

### 6.3 Page

A table with time, server, tool, outcome, total time and the trace id. Expanding a row shows the timing breakdown. If `PORTUNUS_TRACE_URL_TEMPLATE` is set (a URL containing `{trace_id}`), the trace id links to it; otherwise it is copyable text.

### 6.4 Acceptance

1. A failed tool call appears with the same `trace_id` that the agent received in `_meta`.
2. A call passing a sentinel secret as an argument leaves that string in no `tool_calls` row.
3. With trace export off, rows are still written and the page still works.
4. A database error during the insert does not change the call's result.

## 7. Dashboard

- `View` in `App.tsx` gains `fleet` and `logs`. Navigation becomes Apps, Agent keys, Fleet, Logs.
- `PRODUCT.md` currently describes the product as "Apps + Keys". Update it when these pages ship. The rest holds: one operator, real operational data only, no decorative analytics.

## 8. Deferred

- Controls on Fleet: draining a site, stopping a child. Draining needs a scheduler change.
- Live log tailing and container logs. Those stay with the host.
- Charts of per-runner metrics. Use the tracing backend for those.
