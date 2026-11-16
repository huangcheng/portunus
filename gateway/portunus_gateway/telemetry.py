"""OpenTelemetry setup and helpers for the gateway (attribution design).

Export is optional (``OTEL_EXPORTER_OTLP_ENDPOINT``). A TracerProvider is always installed so
every request still gets a real ``trace_id`` even when nothing is exported.

Spans, metrics and call logs must never carry tool arguments, results, env values, tokens, or
URLs with query strings. Attribute names live here so MCP convention renames are one-file edits.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.context import Context, attach, detach
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

log = logging.getLogger("portunus.gateway.telemetry")

# Attribute names (OpenTelemetry MCP conventions are Development-stability).
ATTR_MCP_METHOD = "mcp.method.name"
ATTR_GEN_AI_TOOL = "gen_ai.tool.name"
ATTR_ERROR_TYPE = "error.type"
ATTR_SERVER = "portunus.server"
ATTR_TOOL = "portunus.tool"
ATTR_KEY_ID = "portunus.key_id"
ATTR_SITE_ID = "portunus.site_id"
ATTR_COLD_START = "portunus.cold_start"
ATTR_PREFERRED_MISS = "portunus.preferred_miss"
ATTR_QUEUE_MS = "portunus.queue_ms"
ATTR_INIT_MS = "portunus.init_ms"
ATTR_UPSTREAM_MS = "portunus.upstream_ms"
ATTR_OUTCOME = "portunus.outcome"

_SERVICE_NAME = "portunus-gateway"
_propagator = TraceContextTextMapPropagator()
_configured = False

_tracer = trace.get_tracer(_SERVICE_NAME)
_meter = metrics.get_meter(_SERVICE_NAME)

_op_duration = None
_tool_duration = None
_init_duration = None
_runner_connected = None
_preferred_miss = None


def setup_telemetry(*, service_version: str | None = None) -> None:
    """Install TracerProvider / MeterProvider. Idempotent. Safe with export unset."""
    global _configured, _tracer, _meter
    global _op_duration, _tool_duration, _init_duration, _runner_connected, _preferred_miss
    if _configured:
        return

    attrs: dict[str, str] = {"service.name": _SERVICE_NAME}
    if service_version:
        attrs["service.version"] = service_version
    resource = Resource.create(attrs)

    # Roots always record (real trace ids); honor sampled=00 on inbound traceparent.
    tracer_provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        try:
            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            # Tempo accepts traces only. Set OTEL_METRICS_EXPORTER=none (or a dedicated
            # OTEL_EXPORTER_OTLP_METRICS_ENDPOINT) when the shared endpoint is traces-only.
            metrics_exporter = (os.environ.get("OTEL_METRICS_EXPORTER") or "").strip().lower()
            metrics_endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or "").strip()
            metric_readers = []
            if metrics_exporter != "none":
                metric_kw = {}
                if metrics_endpoint:
                    metric_kw["endpoint"] = metrics_endpoint
                metric_readers = [PeriodicExportingMetricReader(OTLPMetricExporter(**metric_kw))]
            meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
            # Log host only — endpoint may embed userinfo credentials.
            host = endpoint.split("?")[0]
            if "@" in host:
                host = host.split("@", 1)[-1]
            log.info(
                "OTLP export enabled endpoint=%s metrics=%s",
                host,
                "on" if metric_readers else "off",
            )
        except Exception:
            # Bad OTEL_* config must never block gateway startup.
            log.warning("OTLP exporter setup failed; continuing without export", exc_info=True)
            meter_provider = MeterProvider(resource=resource)
    else:
        meter_provider = MeterProvider(resource=resource)
        log.debug("OTLP export disabled; spans still create local trace ids")

    try:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        _tracer = trace.get_tracer(_SERVICE_NAME)
        _meter = metrics.get_meter(_SERVICE_NAME)

        _op_duration = _meter.create_histogram(
            "mcp.server.operation.duration", unit="s", description="MCP tools/call wall time"
        )
        _tool_duration = _meter.create_histogram(
            "portunus.tool.duration", unit="s", description="portunus_invoke_tool wall time"
        )
        _init_duration = _meter.create_histogram(
            "portunus.tool.init.duration", unit="s", description="Child MCP handshake time"
        )
        # Wired from Fleet later; create now so set_runner_connected is a no-op-safe export.
        _runner_connected = _meter.create_gauge(
            "portunus.runner.connected", description="Connected runners by site"
        )
        _preferred_miss = _meter.create_counter(
            "portunus.schedule.preferred_miss",
            description="Starts scheduled outside preferred regions",
        )
    except Exception:
        log.warning("telemetry instrument setup failed; tracing ids still work", exc_info=True)
        _op_duration = _tool_duration = _init_duration = None
        _runner_connected = _preferred_miss = None
    _configured = True


def current_trace_id() -> str:
    """32-hex trace id of the current span, or a new root span's id if none is active."""
    setup_telemetry()
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    with _tracer.start_as_current_span("portunus.trace_id") as fresh:
        return format(fresh.get_span_context().trace_id, "032x")


def extract_parent_context(
    request_meta: Mapping[str, Any] | None = None,
    http_headers: Mapping[str, str] | None = None,
) -> Context:
    """Parent context: params ``_meta.traceparent`` first, else HTTP ``traceparent``, else root."""
    setup_telemetry()
    carrier: dict[str, str] = {}
    if request_meta:
        tp = request_meta.get("traceparent")
        if isinstance(tp, str) and tp.strip():
            carrier["traceparent"] = tp.strip()
            ts = request_meta.get("tracestate")
            if isinstance(ts, str) and ts.strip():
                carrier["tracestate"] = ts.strip()
            return _propagator.extract(carrier)
    if http_headers:
        # Headers may be lower-cased already; accept either.
        tp = http_headers.get("traceparent") or http_headers.get("Traceparent")
        if isinstance(tp, str) and tp.strip():
            carrier["traceparent"] = tp.strip()
            ts = http_headers.get("tracestate") or http_headers.get("Tracestate")
            if isinstance(ts, str) and ts.strip():
                carrier["tracestate"] = ts.strip()
            return _propagator.extract(carrier)
    return Context()


@contextmanager
def use_parent_context(parent: Context) -> Iterator[None]:
    token = attach(parent)
    try:
        yield
    finally:
        detach(token)


@contextmanager
def start_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    setup_telemetry()
    with _tracer.start_as_current_span(name, kind=kind, attributes=dict(attributes or {})) as span:
        yield span


def set_span_error(span: Span, error_type: str) -> None:
    span.set_attribute(ATTR_ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR, error_type))


def record_operation_duration(
    seconds: float, *, method: str, tool: str, error_type: str | None = None
) -> None:
    setup_telemetry()
    if _op_duration is None:
        return
    attrs: dict[str, str] = {ATTR_MCP_METHOD: method, ATTR_GEN_AI_TOOL: tool}
    if error_type:
        attrs[ATTR_ERROR_TYPE] = error_type
    _op_duration.record(seconds, attrs)


def record_tool_duration(
    seconds: float, *, server: str, tool: str, outcome: str
) -> None:
    setup_telemetry()
    if _tool_duration is None:
        return
    _tool_duration.record(
        seconds, {ATTR_SERVER: server, ATTR_TOOL: tool, ATTR_OUTCOME: outcome}
    )


def record_init_duration(seconds: float, *, server: str) -> None:
    setup_telemetry()
    if _init_duration is None:
        return
    _init_duration.record(seconds, {ATTR_SERVER: server})


def record_preferred_miss(*, server: str) -> None:
    setup_telemetry()
    if _preferred_miss is None:
        return
    _preferred_miss.add(1, {ATTR_SERVER: server})


def set_runner_connected(site_id: str, connected: int) -> None:
    setup_telemetry()
    if _runner_connected is None:
        return
    _runner_connected.set(connected, {ATTR_SITE_ID: site_id})


# Re-export for callers that need SpanKind without a second import.
__all__ = [
    "ATTR_COLD_START",
    "ATTR_ERROR_TYPE",
    "ATTR_GEN_AI_TOOL",
    "ATTR_INIT_MS",
    "ATTR_KEY_ID",
    "ATTR_MCP_METHOD",
    "ATTR_OUTCOME",
    "ATTR_PREFERRED_MISS",
    "ATTR_QUEUE_MS",
    "ATTR_SERVER",
    "ATTR_SITE_ID",
    "ATTR_TOOL",
    "ATTR_UPSTREAM_MS",
    "SpanKind",
    "current_trace_id",
    "extract_parent_context",
    "record_init_duration",
    "record_operation_duration",
    "record_preferred_miss",
    "record_tool_duration",
    "set_runner_connected",
    "set_span_error",
    "setup_telemetry",
    "start_span",
    "use_parent_context",
]
