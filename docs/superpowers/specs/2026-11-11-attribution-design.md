# Portunus — Call Attribution (tracing, timing, build identity)

**Date:** 2026-11-11  
**Status:** design  
**Covers:** assessment P0-1 (trace id + timing on every response) and P1-5 (build identity).  
**Related:** `2026-11-11-server-state-design.md`, `2026-11-11-fleet-logs-design.md`.

## 1. Problem

A tool call through `/mcp` returns a result or an error and nothing else. When a call is slow, neither the agent nor the operator can tell whether the time went to the gateway, to starting a child process, to waiting behind another call, or to the upstream service. The assessment hit this with a `koofr` listing that took 47 s once and 2–3 s afterwards, and could only guess at the cause.

## 2. What the code does today (checked 2026-11-11)

- `Omni.dispatch` in `gateway/portunus_gateway/omni.py` records nothing: no log line, no timing, no id. Its `_clock` is used only for the rate-limit window.
- Results are built by `ok_result`, `error_result` and `finish_result`. None of them set `_meta`. The MCP SDK in use (`mcp` 2.3.0) supports it: `CallToolResult.meta` serialises as `_meta`, and `CallToolRequestParams.meta` carries the caller's `_meta`.
- Errors raised before MCP handling (401, 413, bad envelope) are built by `_error_response` and `_rpc_error_response`. They carry `error.data.code` at most.
- Stdio servers: `SlotManager.reserve_and_start` reuses the child for a (server, key) pair or starts a new one. The runner's `start` only spawns the process. `ChildRpc.call` in `runner/portunus_runner/rpc.py` runs the child's MCP handshake lazily on the first call, and serialises calls per child behind a lock.
- Consequence: a cold start (for example an `npx` package download) and time spent waiting behind another call both happen inside the first `rpc` round trip. From the gateway they look the same as a slow upstream. This fits the 47 s `koofr` call, but nothing recorded it, so that cause is unconfirmed.
- Remote servers go through `RemoteProxy.invoke`, also untimed.
- Gateway and runner frames accept extra fields: `wire._msg(**fields)` passes them through and the runner reads with `msg.get(...)`. New reply fields need no wire version bump; an old runner simply omits them.
- `serverInfo.version` is the package version, `0.1.0`. Deploys so far were file overlays (rsync and `docker cp`), which never change it.
- There is no OpenTelemetry dependency and no `OTEL_*` variable in production.

## 3. Decisions

1. **The gateway owns tracing.** Only the gateway gets the OpenTelemetry SDK. The runner reports timing numbers in its replies and the gateway attaches them to its spans. Runner-side spans are deferred.
2. **Export is OTLP and optional.** Traces and metrics are exported when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. There is no Prometheus scrape endpoint in this version.
3. **A trace id is always present.** With export off, the gateway still creates trace ids, returns them, and writes them to its log.
4. **Gateway metadata goes in `_meta`.** It does not go in `structuredContent`, which belongs to the upstream tool's payload.
5. **Build identity is a source fingerprint**, not a hand-maintained version (§8).

## 4. Trace context

The parent context for an MCP request is taken from, in order:

1. `params._meta.traceparent` (plus `tracestate`), as the OpenTelemetry MCP conventions specify;
2. the HTTP `traceparent` header;
3. otherwise a new root trace.

Every request is sampled. The gateway serves one operator and the volume is bounded by the per-key rate limit.

Spans:

| Span | Kind | Notable attributes |
|---|---|---|
| `tools/call {meta tool}` | server | `mcp.method.name`, `gen_ai.tool.name`, `error.type` on failure |
| `portunus.start_child` | internal | `portunus.site_id`, `portunus.cold_start`, `portunus.preferred_miss` |
| `portunus.runner.rpc` | client | `portunus.site_id`, `portunus.queue_ms`, `portunus.init_ms`, `portunus.upstream_ms` |
| `portunus.remote.invoke` | client | `portunus.server`, HTTP status class |

Every span below the server span also carries `portunus.server` (slug), `portunus.tool` (upstream tool name) and `portunus.key_id` (the public key id).

Spans, metrics and log lines never contain tool arguments, tool results, environment values, tokens, or URLs with query strings.

The OpenTelemetry MCP conventions are at "Development" stability, so attribute names may change. The names live in one module so a rename is a one-file edit.

## 5. Timing fields

| Field | Measured by | Meaning | Present when |
|---|---|---|---|
| `gateway_ms` | gateway | Wall time of the whole `dispatch` | always |
| `start_ms` | gateway | Time inside `reserve_and_start` | stdio invoke |
| `cold_start` | gateway | A new child was started for this call | stdio invoke |
| `queue_ms` | runner | Time waiting for the per-child lock | stdio invoke, runner reports it |
| `init_ms` | runner | Child MCP handshake, including any package download | first call on a child, runner reports it |
| `upstream_ms` | runner or gateway | The `tools/call` itself | any invoke that reached the upstream |

For remote servers `upstream_ms` is the wall time of `RemoteProxy.invoke`. For a runner that does not report timing, `upstream_ms` is the full `rpc` round trip and `queue_ms` and `init_ms` are absent.

## 6. Response contract

Every `CallToolResult`, success or error, carries:

```json
"_meta": {
  "portunus": {
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "gateway_ms": 2140,
    "start_ms": 12,
    "cold_start": false,
    "queue_ms": 0,
    "upstream_ms": 2103
  }
}
```

Discovery calls carry `trace_id` and `gateway_ms` only. Errors raised before MCP handling carry `error.data.trace_id`.

`site_id` is not returned to agents. The fleet design keeps scheduling details out of the agent's view; the site is recorded in the tool-call log instead.

## 7. Log line and metrics

One INFO line per `portunus_invoke_tool` on logger `portunus.gateway.calls`, as `key=value` pairs: `trace_id`, `key_id`, `server`, `tool`, `outcome`, `site_id`, and the timing fields. This is what makes a trace id findable without a collector.

Metrics, exported over OTLP:

| Instrument | Type | Attributes |
|---|---|---|
| `mcp.server.operation.duration` | histogram, s | `mcp.method.name`, `gen_ai.tool.name`, `error.type` |
| `portunus.tool.duration` | histogram, s | `portunus.server`, `portunus.tool`, `portunus.outcome` |
| `portunus.tool.init.duration` | histogram, s | `portunus.server` |
| `portunus.runner.connected` | gauge | `portunus.site_id` |
| `portunus.schedule.preferred_miss` | counter | `portunus.server` |

## 8. Build identity

A fingerprint is the first 12 hex characters of a SHA-256 over the sorted relative paths and contents of every `.py` file in a package directory, computed once at process start.

- `serverInfo.version` becomes `<package version>+<fingerprint of portunus_gateway>`, for example `0.1.0+3fa9c1d2ab77`.
- The helper lives in `portunus_shared` so the runner can report its own fingerprints (see the Fleet spec).

This works for overlay deploys as well as image builds, because it reads the code that is actually importable. One difference from the assessment's wording: redeploying identical code yields the same version. That is intended, since it is the same build.

## 9. Changes by component

| Component | Change |
|---|---|
| `gateway/pyproject.toml` | Add `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` |
| `gateway/portunus_gateway/telemetry.py` (new) | Provider setup from env, attribute names, span and metric helpers |
| `omni.py` | Time `dispatch`; attach `_meta` in one place; spans around start, rpc and remote invoke; call log line; `trace_id` in `_error_response` |
| `slots.py` | `reserve_and_start` reports whether it started a new child |
| `runner/portunus_runner/rpc.py`, `session.py` | Measure `queue_ms`, `init_ms`, `upstream_ms`; include a `timing` object in `rpc` replies and in error replies the runner produced |
| `portunus_shared` | Fingerprint helper |
| `gateway/portunus_gateway/__init__.py` | Version with fingerprint |

## 10. Acceptance tests

1. A fake upstream delayed by 2 s yields `upstream_ms ≥ 1900`, and a record on `portunus.gateway.calls` has the same `trace_id`.
2. The first call on a fresh child whose handshake is delayed by 1 s yields `cold_start: true` and `init_ms ≥ 900`, with `upstream_ms` excluding that time.
3. Two concurrent calls for the same (server, key) pair: the second reports `queue_ms` close to the first call's duration.
4. A request with `params._meta.traceparent` gets that trace id back. With only the HTTP header, it gets the header's trace id.
5. A 401 and a 413 each carry `error.data.trace_id`.
6. A runner reply without `timing` produces a result with `upstream_ms` and no `queue_ms` or `init_ms`.
7. With an in-memory span exporter, a call passing a sentinel secret as an argument leaves that string in no span attribute and no log record.
8. Changing a gateway source file changes `serverInfo.version`.

## 11. Deferred

- Spans created inside the runner.
- Passing trace context on to upstream MCP servers. For remote servers this would send trace ids to third parties, so it stays off until there is a reason.
- A Prometheus scrape endpoint.
