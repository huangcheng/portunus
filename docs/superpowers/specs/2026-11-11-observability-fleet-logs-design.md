# Portunus — Observability, Fleet and Logs (index)

**Date:** 2026-11-11  
**Status:** index. The design is in the three specs below.  
**Inputs:** operator direction (OpenTelemetry, Fleet and Logs pages); the black-box assessment of 2026-11-11; `2026-11-09-portunus-fleet-design.md`.

Portunus is a self-hosted gateway for one operator. Observability here means the operator can explain a slow or failed call and see the state of their own runners. It does not mean product analytics, tenants, or billing.

## Specs

| Spec | What it settles | Assessment items |
|---|---|---|
| [`2026-11-11-attribution-design.md`](2026-11-11-attribution-design.md) | Trace id and timing breakdown on every response; OpenTelemetry in the gateway; build identity | P0-1, P1-5 |
| [`2026-11-11-server-state-design.md`](2026-11-11-server-state-design.md) | Discovery `status` that follows what the gateway observed | P0-2, P1-2 |
| [`2026-11-11-fleet-logs-design.md`](2026-11-11-fleet-logs-design.md) | Fleet page; Logs page with Audit and Tool calls | none directly |

## Order

Attribution and server state are independent of each other and can be built in either order.

The Fleet page and the Audit tab depend on neither, because runner connectivity and audit rows are already real data. They can ship first.

The Tool calls tab needs attribution. Server status chips need server state.

## Decisions made across the three

- Tracing lives in the gateway only. Runners report timing numbers in their replies.
- Export is OTLP and optional. A trace id is returned and logged even with export off.
- Tool calls are stored in the gateway's database, so the Logs page does not need a tracing backend.
- There is no Prometheus endpoint and no background prober in this version.
- `degraded` is not a status until it has a definition a test can check.

## Not covered here

These assessment items are still open and have no spec yet:

- Result-size policy and splitting `payload_too_large` into request and result codes (P0-3, P0-4).
- A test that iterates the error-code registry (P1-1).
- Cursor binding to server and query (P1-3).
- Structured result type instead of a JSON string (P1-4).
- Write-path governance: read-only keys and grants for mutating tools (P2-1).
- Schema access in fewer round trips, and the `schema_pin` value (P2-3, P2-4).

P2-2 (one source of truth for registry and grants) is not carried forward. Production runs a single gateway on Postgres. Only the gateway's configured backend was checked; if an older second database still exists anywhere, it should be retired.
