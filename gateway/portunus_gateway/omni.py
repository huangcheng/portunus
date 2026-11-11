"""Omni ``/mcp`` (spec §5): Streamable HTTP front door with exactly four ``portunus_*`` meta tools.

Invariants
----------
* Every request carries ``Authorization: Bearer pm-...``; it is verified per request (stateless
  transport, no session to outlive a revoke). The gate also caps the request body at 1MiB.
* Upstream tools are never flattened into ``tools/list``: agents ``discover`` -> ``get_tool_schema``
  -> ``invoke``. Disabled servers/tools and ungranted servers are invisible to discovery.
* Authorization order everywhere: ``is_revoked`` -> ``has_grant`` -> server enabled -> tool
  exists -> tool enabled. (``AuthzStore.has_grant`` keeps answering for revoked keys.) An unknown
  server answers ``not_granted`` so slugs cannot be enumerated.
* ``invoke`` validates arguments against the *stored, operator-approved* schema before any child is
  started, optionally pinned by ``schema_hash``.
* A tool result over 1MiB (serialized) is **never** returned, not even truncated: the agent gets a
  ``payload_too_large`` error. The runner enforces its own larger bound (and kills the child).
* A result is only handed back if the key is still authorized and the child was not marked
  ``pending_stop`` while the call ran (revoke / ungrant / disable / vault bump mid-call).
* A request body that is a JSON-RPC batch is refused with ``-32600`` before it reaches the
  transport, which would otherwise leak a pydantic traceback instead of a protocol error.
  Other envelope validation failures are refused here with ``-32602`` for the same reason.
* HTTP-layer guards (auth, body size) use the same JSON-RPC error envelope as the transport,
  with the Portunus string code preserved in ``error.data.code``.
* ``serverInfo.version`` is the package version, never blank.
* Errors reach the agent as ``isError`` tool results whose JSON text is
  ``{"error": {"code": <spec §6.4 code>, "message": ...}}``.
* When an upstream result has no ``structuredContent`` but ``content[0].text`` is JSON, it is
  promoted so remote and stdio results share one shape.
* Limits: 60 tool calls/min/key (``rate_limited``), 8 concurrent invokes/key (``rate_limited``),
  invoke timeout default 60s (an agent may only shorten it). A timed-out or cancelled call retires
  its child (it may be wedged mid-call).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

import mcp_types as types
from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from opentelemetry.trace import SpanKind
from portunus_shared import DEFAULTS, ERROR_CODES, PortunusError
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from . import __version__
from . import telemetry as tel
from .db.models import Server as ServerRow
from .db.models import Tool as ToolRow
from .keys import AuthError, verify_agent_token
from .registry import RegistryError
from .runner_client import RunnerRequestError
from .runner_pool import RunnerPool

log_calls = logging.getLogger("portunus.gateway.calls")

# Invoke handlers stash timing fields here; dispatch merges them into ``_meta.portunus``.
_invoke_attrs: ContextVar[dict[str, Any] | None] = ContextVar("portunus_invoke_attrs", default=None)

MAX_BODY_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
PAGE_DEFAULT = 50
PAGE_MAX = 100
RPC_SLACK_S = 5.0  # runner enforces timeout_s itself; leave room for its reply
KEY_SCOPE = "portunus_key_id"

TOOL_NAMES = (
    "portunus_discover_servers",
    "portunus_discover_tools",
    "portunus_get_tool_schema",
    "portunus_invoke_tool",
)

_STR = {"type": "string"}
_PAGING = {
    "query": {"type": "string", "description": "Case-insensitive substring filter."},
    "cursor": {"type": "string", "description": "Opaque `next_cursor` from the previous page."},
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": PAGE_MAX,
        "description": f"Page size (default {PAGE_DEFAULT}, max {PAGE_MAX}).",
    },
}


def _tool_schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_SCHEMAS: dict[str, dict[str, Any]] = {
    "portunus_discover_servers": _tool_schema(dict(_PAGING)),
    "portunus_discover_tools": _tool_schema(
        {"server": {**_STR, "description": "Server slug."}, **_PAGING}, ["server"]
    ),
    "portunus_get_tool_schema": _tool_schema(
        {"server": {**_STR, "description": "Server slug."}, "tool": {**_STR}}, ["server", "tool"]
    ),
    "portunus_invoke_tool": _tool_schema(
        {
            "server": {**_STR, "description": "Server slug."},
            "tool": _STR,
            "arguments": {"type": "object", "description": "Arguments per the tool's schema."},
            "schema_hash": {
                **_STR,
                "description": (
                    "Optional pin from portunus_get_tool_schema. When omitted, drift is not "
                    "checked; when set, a changed schema returns schema_drift before invoke."
                ),
            },
            "timeout_s": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": float(DEFAULTS.invoke_timeout_s),
                "description": (
                    f"Optional: shorten (never extend) the invoke timeout "
                    f"(max {DEFAULTS.invoke_timeout_s}s)."
                ),
            },
        },
        ["server", "tool"],
    ),
}
_DESCRIPTIONS = {
    "portunus_discover_servers": (
        "List the MCP servers this key may use (paged). Each item includes a derived "
        "status (probing|ok|auth_required|unreachable), tool_count (null until the first "
        "successful probe), and last_probe_at / last_ok_at / last_error_* timestamps. "
        "An unreachable status reflects the last observation; a retry is allowed."
    ),
    "portunus_discover_tools": "List the enabled tools of a granted server (paged).",
    "portunus_get_tool_schema": "Get the input schema and description of one tool.",
    "portunus_invoke_tool": "Invoke one tool of a granted server with arguments.",
}


def list_tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(name=n, description=_DESCRIPTIONS[n], input_schema=_SCHEMAS[n]) for n in TOOL_NAMES
    ]


# ---- results ---------------------------------------------------------------------------


def error_result(
    code: str, message: str = "", *, data: dict[str, Any] | None = None
) -> types.CallToolResult:
    if code not in ERROR_CODES:
        code, message = "upstream_crash", message or "internal error"
    err: dict[str, Any] = {"code": code, "message": message}
    if data:
        err["data"] = data
    body = {"error": err}
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(body))], structured_content=body, is_error=True
    )


def ok_result(obj: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(obj))], structured_content=obj
    )


def with_portunus_meta(
    result: types.CallToolResult, portunus: Mapping[str, Any]
) -> types.CallToolResult:
    """Merge gateway attribution into ``result.meta`` (wire alias ``_meta``), never structuredContent."""
    meta = dict(result.meta) if result.meta else {}
    existing = meta.get("portunus")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(portunus)
    meta["portunus"] = merged
    return result.model_copy(update={"meta": meta})


def _ms_since(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))


def _error_code_from_result(result: types.CallToolResult) -> str | None:
    if not result.is_error:
        return None
    structured = result.structured_content
    if isinstance(structured, dict):
        err = structured.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return err["code"]
    return "upstream_error"


def _safe_label(value: str, *, max_len: int = 128) -> str:
    """Strip control chars and truncate so client input cannot forge log lines / metric labels."""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:max_len]


def _log_invoke_call(
    *,
    trace_id: str,
    key_id: str,
    server: str,
    tool: str,
    outcome: str,
    site_id: str | None,
    timing: Mapping[str, Any],
) -> None:
    server = _safe_label(server)
    tool = _safe_label(tool)
    parts = [
        f"trace_id={trace_id}",
        f"key_id={key_id}",
        f"server={server}",
        f"tool={tool}",
        f"outcome={outcome}",
    ]
    if site_id is not None:
        parts.append(f"site_id={site_id}")
    for key in (
        "gateway_ms",
        "start_ms",
        "cold_start",
        "queue_ms",
        "init_ms",
        "upstream_ms",
    ):
        if key in timing:
            parts.append(f"{key}={timing[key]}")
    log_calls.info(" ".join(parts))


def _promote_structured(content: list[Any], structured: Any) -> Any:
    """If upstream omitted ``structuredContent`` but text is JSON, promote it (review #19)."""
    if structured is not None or not content:
        return structured
    first = content[0]
    text = getattr(first, "text", None)
    if not isinstance(text, str) or not text.strip():
        return structured
    try:
        promoted = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return structured
    if isinstance(promoted, dict):
        return promoted
    if isinstance(promoted, list):
        return {"items": promoted}
    return {"value": promoted}


def _already_gateway_error(structured: Any) -> bool:
    if not isinstance(structured, dict):
        return False
    err = structured.get("error")
    return isinstance(err, dict) and err.get("code") in ERROR_CODES


def _first_text(content: list[Any]) -> str:
    if not content:
        return ""
    text = getattr(content[0], "text", None)
    return text if isinstance(text, str) else ""


def finish_result(
    result: Any, *, server: str | None = None, tool: str | None = None
) -> types.CallToolResult:
    """Turn an upstream ``tools/call`` result into the agent-facing result.

    Over 1MiB serialized -> ``payload_too_large`` (the payload is dropped entirely: an agent must
    never mistake a truncated payload for a successful one).

    Proxied ``isError`` results without a gateway envelope become ``upstream_error``
    (review #16 / #19-failure) while keeping the original ``content[0].text``.
    """
    if not isinstance(result, dict):
        return error_result("upstream_crash", "malformed tool result")
    size = len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())
    if size > MAX_RESULT_BYTES:
        return error_result(
            "payload_too_large", f"tool result exceeds the {MAX_RESULT_BYTES} byte limit"
        )
    try:
        parsed = types.CallToolResult.model_validate(result)
    except Exception:
        return error_result("upstream_crash", "malformed tool result")
    structured = _promote_structured(list(parsed.content), parsed.structured_content)
    if parsed.is_error and not _already_gateway_error(structured):
        text = _first_text(list(parsed.content))
        message = (text.strip().split("\n", 1)[0] if text.strip() else "")[:240] or (
            "upstream tool reported an error"
        )
        upstream: dict[str, Any] = {}
        if server:
            upstream["server"] = server
        if tool:
            upstream["tool"] = tool
        body = {
            "error": {
                "code": "upstream_error",
                "message": message,
                **({"upstream": upstream} if upstream else {}),
            }
        }
        return types.CallToolResult(
            content=parsed.content, structured_content=body, is_error=True
        )
    return types.CallToolResult(
        content=parsed.content, structured_content=structured, is_error=parsed.is_error
    )


# ---- paging ----------------------------------------------------------------------------


def encode_cursor(after: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"after": after}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        after = json.loads(raw)["after"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise PortunusError("invalid_cursor", "invalid cursor") from None
    if not isinstance(after, str):
        raise PortunusError("invalid_cursor", "invalid cursor")
    return after


def _page(items: list[dict[str, Any]], key: str, cursor: str | None, limit: int) -> dict[str, Any]:
    """Keyset page over ``items`` sorted by ``item[key]``."""
    after = decode_cursor(cursor)
    rest = [i for i in items if after is None or i[key] > after]
    page = rest[:limit]
    more = len(rest) > limit
    return {"items": page, "next_cursor": encode_cursor(page[-1][key]) if more else None}


# ---- server state seen by the (synchronous) slot predicates ------------------------------


class ServerStateCache:
    """Last-seen ``(enabled, vault_version)`` per server, for ``SlotManager``'s sync predicates.

    Refreshed by every invoke from the database. Unknown servers fail closed (not enabled).
    """

    def __init__(self) -> None:
        self._state: dict[uuid.UUID, tuple[bool, int | None]] = {}

    def put(self, server_id: uuid.UUID, enabled: bool, vault_version: int | None) -> None:
        self._state[server_id] = (enabled, vault_version)

    def enabled(self, server_id: uuid.UUID) -> bool:
        return self._state.get(server_id, (False, None))[0]

    def vault_version(self, server_id: uuid.UUID) -> int | None:
        return self._state.get(server_id, (False, None))[1]


# ---- Omni ------------------------------------------------------------------------------


def _first_error(exc: ValidationError) -> str:
    path = "/".join(str(p) for p in exc.absolute_path)
    return f"{path or '<root>'}: {exc.message}"[:300]


def _validate(schema: dict[str, Any], value: Any, *, what: str) -> None:
    if what == "tool arguments":
        code = "invalid_tool_arguments"
    elif what == "arguments":
        code = "invalid_gateway_arguments"
    else:
        code = "schema_mismatch"
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        cls(schema).validate(value)
    except ValidationError as e:
        raise PortunusError(code, f"{what}: {_first_error(e)}") from None
    except SchemaError:
        raise PortunusError(code, f"{what}: stored schema is not valid") from None


class Omni:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        registry: Any,
        slots: Any,
        runner: Any,
        authz: Any,
        state_cache: ServerStateCache | None = None,
        invoke_timeout_s: float = DEFAULTS.invoke_timeout_s,
        max_concurrent: int = 8,
        rate_per_min: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sf = session_factory
        self.registry = registry
        self.slots = slots
        self.runner = runner
        self.pool = RunnerPool.ensure(runner)
        self.authz = authz
        self.state_cache = state_cache or ServerStateCache()
        self.invoke_timeout_s = invoke_timeout_s
        self.max_concurrent = max_concurrent
        self.rate_per_min = rate_per_min
        self._clock = clock
        self._inflight: dict[str, int] = {}
        self._calls: dict[str, deque[float]] = {}

        self.server = Server(
            "portunus",
            version=__version__,
            instructions=(
                "Portunus MCP gateway. Use portunus_discover_servers / portunus_discover_tools / "
                "portunus_get_tool_schema, then portunus_invoke_tool."
            ),
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )
        self.manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=True,
            stateless=True,
            max_request_body_size=MAX_BODY_BYTES,
        )
        self.asgi = _Gate(self.manager)

    # ---- MCP handlers --------------------------------------------------------------

    async def _on_list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list_tool_definitions())

    async def _on_call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        request = getattr(ctx, "request", None)
        key_id = request.scope.get(KEY_SCOPE) if request is not None else None
        http_headers: dict[str, str] = {}
        if request is not None:
            for raw_k, raw_v in request.scope.get("headers") or ():
                # ASGI headers are always (bytes, bytes); latin-1 cannot fail on bytes.
                if isinstance(raw_k, (bytes, bytearray)) and isinstance(raw_v, (bytes, bytearray)):
                    http_headers[raw_k.decode("latin-1").lower()] = raw_v.decode("latin-1")
        request_meta: Mapping[str, Any] | None = None
        if params.meta:
            request_meta = dict(params.meta)
        if not isinstance(key_id, str):
            # Still mint a trace id so clients can correlate the unauthorized result.
            parent = tel.extract_parent_context(request_meta, http_headers)
            with tel.use_parent_context(parent):
                with tel.start_span(
                    "tools/call unknown",
                    kind=SpanKind.SERVER,
                    attributes={
                        tel.ATTR_MCP_METHOD: "tools/call",
                        tel.ATTR_GEN_AI_TOOL: "unknown",
                    },
                ) as span:
                    tel.set_span_error(span, "unauthorized")
                    return with_portunus_meta(
                        error_result("unauthorized", "no authenticated agent key"),
                        {"trace_id": tel.current_trace_id(), "gateway_ms": 0},
                    )
        return await self.dispatch(
            key_id,
            params.name,
            params.arguments or {},
            request_meta=request_meta,
            http_headers=http_headers,
        )

    # ---- dispatch ------------------------------------------------------------------

    def inflight(self, key_id: str) -> int:
        return self._inflight.get(key_id, 0)

    def _rate_limit(self, key_id: str) -> None:
        now = self._clock()
        window = self._calls.setdefault(key_id, deque())
        while window and window[0] <= now - 60.0:
            window.popleft()
        if len(window) >= self.rate_per_min:
            raise PortunusError("rate_limited", f"over {self.rate_per_min} calls per minute")
        window.append(now)

    async def dispatch(
        self,
        key_id: str,
        name: str,
        args: dict[str, Any],
        *,
        request_meta: Mapping[str, Any] | None = None,
        http_headers: Mapping[str, str] | None = None,
    ) -> types.CallToolResult:
        """Run one meta tool for an authenticated key. Never raises for expected failures."""
        t0 = time.perf_counter()
        parent = tel.extract_parent_context(request_meta, http_headers)
        invoke_attrs: dict[str, Any] = {}
        token = _invoke_attrs.set(invoke_attrs)
        # Bound metric/span labels before validating the name (unknown tools are rejected later).
        tool_label = _safe_label(name) if name in _SCHEMAS else "unknown"
        try:
            with tel.use_parent_context(parent):
                with tel.start_span(
                    f"tools/call {tool_label}",
                    kind=SpanKind.SERVER,
                    attributes={
                        tel.ATTR_MCP_METHOD: "tools/call",
                        tel.ATTR_GEN_AI_TOOL: tool_label,
                        tel.ATTR_KEY_ID: key_id,
                    },
                ) as span:
                    try:
                        if name not in _SCHEMAS:
                            raise PortunusError("unsupported", f"unknown tool {name!r}")
                        self._check_key(key_id)
                        _validate(_SCHEMAS[name], args, what="arguments")
                        self._rate_limit(key_id)
                        handler = {
                            "portunus_discover_servers": self._discover_servers,
                            "portunus_discover_tools": self._discover_tools,
                            "portunus_get_tool_schema": self._get_tool_schema,
                            "portunus_invoke_tool": self._invoke,
                        }[name]
                        result = await handler(key_id, args)
                    except PortunusError as e:
                        tel.set_span_error(span, e.code)
                        result = error_result(e.code, e.message)
                    except RegistryError as e:
                        code = e.code if e.code in ERROR_CODES else "spawn_failed"
                        tel.set_span_error(span, code)
                        result = error_result(code, e.message)

                    gateway_ms = _ms_since(t0)
                    portunus: dict[str, Any] = {
                        "trace_id": tel.current_trace_id(),
                        "gateway_ms": gateway_ms,
                    }
                    # Invoke fills start/cold/queue/init/upstream; discovery keeps only the above.
                    for key in (
                        "start_ms",
                        "cold_start",
                        "queue_ms",
                        "init_ms",
                        "upstream_ms",
                    ):
                        if key in invoke_attrs:
                            portunus[key] = invoke_attrs[key]

                    error_type = _error_code_from_result(result)
                    if error_type:
                        tel.set_span_error(span, error_type)
                    tel.record_operation_duration(
                        gateway_ms / 1000.0,
                        method="tools/call",
                        tool=tool_label,
                        error_type=error_type,
                    )
                    if name == "portunus_invoke_tool":
                        # Prefer resolved DB slugs; never trust raw args for logs/metrics.
                        server = _safe_label(str(invoke_attrs.get("server") or ""))
                        tool = _safe_label(str(invoke_attrs.get("tool") or ""))
                        outcome = error_type or "ok"
                        if server and tool:
                            tel.record_tool_duration(
                                gateway_ms / 1000.0,
                                server=server,
                                tool=tool,
                                outcome=outcome,
                            )
                        init_ms = invoke_attrs.get("init_ms")
                        if isinstance(init_ms, int) and server:
                            tel.record_init_duration(init_ms / 1000.0, server=server)
                        _log_invoke_call(
                            trace_id=portunus["trace_id"],
                            key_id=key_id,
                            server=server or "unknown",
                            tool=tool,
                            outcome=outcome,
                            site_id=invoke_attrs.get("site_id"),
                            timing=portunus,
                        )
                        # Pre-invoke rate_limited never reaches here (server/tool unset).
                        if server and tool:
                            await self._record_tool_call(
                                trace_id=portunus["trace_id"],
                                key_id=key_id,
                                server_slug=server,
                                tool=tool,
                                outcome=outcome,
                                invoke_attrs=invoke_attrs,
                                gateway_ms=gateway_ms,
                                result=result,
                            )
                    return with_portunus_meta(result, portunus)
        finally:
            _invoke_attrs.reset(token)

    async def _record_tool_call(
        self,
        *,
        trace_id: str,
        key_id: str,
        server_slug: str,
        tool: str,
        outcome: str,
        invoke_attrs: Mapping[str, Any],
        gateway_ms: int,
        result: types.CallToolResult,
    ) -> None:
        """Best-effort insert into ``tool_calls``; never affects the agent result."""
        try:
            from .db.models import ToolCall

            meta = getattr(result, "meta", None)
            content = getattr(result, "content", None)
            # Size estimate without a full JSON dump of the CallToolResult.
            try:
                result_bytes = len(repr(content).encode()) if content is not None else None
                if isinstance(meta, Mapping):
                    result_bytes = (result_bytes or 0) + len(repr(meta).encode())
            except Exception:  # noqa: BLE001
                result_bytes = None
            cold = invoke_attrs.get("cold_start")
            pref = invoke_attrs.get("preferred_miss")
            row = ToolCall(
                trace_id=trace_id,
                key_id=key_id,
                server_slug=server_slug,
                tool=tool,
                outcome=outcome,
                site_id=invoke_attrs.get("site_id")
                if isinstance(invoke_attrs.get("site_id"), str)
                else None,
                cold_start=bool(cold) if isinstance(cold, bool) else None,
                preferred_miss=bool(pref) if isinstance(pref, bool) else None,
                gateway_ms=gateway_ms,
                start_ms=invoke_attrs.get("start_ms")
                if isinstance(invoke_attrs.get("start_ms"), int)
                else None,
                queue_ms=invoke_attrs.get("queue_ms")
                if isinstance(invoke_attrs.get("queue_ms"), int)
                else None,
                init_ms=invoke_attrs.get("init_ms")
                if isinstance(invoke_attrs.get("init_ms"), int)
                else None,
                upstream_ms=invoke_attrs.get("upstream_ms")
                if isinstance(invoke_attrs.get("upstream_ms"), int)
                else None,
                result_bytes=result_bytes,
            )
            async with self._sf() as s:
                s.add(row)
                await s.commit()
        except Exception:  # noqa: BLE001 — bookkeeping must not change the call result
            log_calls.exception("tool_calls insert failed")

    # ---- authorization -------------------------------------------------------------

    def _check_key(self, key_id: str) -> None:
        if self.authz.is_revoked(key_id):
            raise PortunusError("revoked", "agent key revoked")

    async def _granted_server(self, key_id: str, slug: str) -> ServerRow:
        """The enabled server ``slug`` if ``key_id`` may use it, else the precise error."""
        self._check_key(key_id)
        async with self._sf() as s:
            server = (
                await s.execute(select(ServerRow).where(ServerRow.slug == slug.casefold()))
            ).scalar_one_or_none()
        if server is None or not self.authz.has_grant(key_id, server.id):
            raise PortunusError("not_granted", "server not granted to this key")
        if not server.enabled:
            raise PortunusError("server_disabled", "server is disabled")
        if server.kind not in ("stdio", "remote"):
            raise PortunusError("unsupported", f"server kind {server.kind!r} is not supported")
        return server

    async def _enabled_tool(self, server: ServerRow, name: str) -> ToolRow:
        async with self._sf() as s:
            tool = (
                await s.execute(
                    select(ToolRow).where(
                        ToolRow.server_id == server.id, ToolRow.upstream_name == name
                    )
                )
            ).scalar_one_or_none()
        if tool is None:
            raise PortunusError("tool_missing", "no such tool")
        if not tool.enabled:
            raise PortunusError("tool_disabled", "tool is disabled")
        return tool

    # ---- discovery -----------------------------------------------------------------

    @staticmethod
    def _paging(args: dict[str, Any]) -> tuple[str, str | None, int]:
        return str(args.get("query", "")).casefold(), args.get("cursor"), int(args.get("limit", PAGE_DEFAULT))

    async def _discover_servers(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        query, cursor, limit = self._paging(args)
        async with self._sf() as s:
            servers = list(
                (await s.execute(select(ServerRow).where(ServerRow.enabled.is_(True))))
                .scalars()
                .all()
            )
        granted = [
            sv
            for sv in sorted(servers, key=lambda x: x.slug)
            if self.authz.has_grant(key_id, sv.id)
            and (
                not query
                or query in sv.slug.casefold()
                or query in sv.name.casefold()
            )
        ]
        health_map = await self.registry.health_by_server_ids(granted)
        items = [
            {
                "slug": sv.slug,
                "name": sv.name,
                "kind": sv.kind,
                **health_map[sv.id],
            }
            for sv in granted
        ]
        return ok_result(_page(items, "slug", cursor, limit))

    async def _discover_tools(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        query, cursor, limit = self._paging(args)
        server = await self._granted_server(key_id, args["server"])
        async with self._sf() as s:
            tools = (
                (
                    await s.execute(
                        select(ToolRow).where(
                            ToolRow.server_id == server.id, ToolRow.enabled.is_(True)
                        )
                    )
                )
                .scalars()
                .all()
            )
        items = [
            {"name": t.upstream_name, "description": t.description, "schema_hash": t.schema_hash}
            for t in sorted(tools, key=lambda x: x.upstream_name)
            if not query
            or query in t.upstream_name.casefold()
            or query in (t.description or "").casefold()
        ]
        return ok_result(_page(items, "name", cursor, limit))

    async def _get_tool_schema(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        server = await self._granted_server(key_id, args["server"])
        tool = await self._enabled_tool(server, args["tool"])
        return ok_result(
            {
                "server": server.slug,
                "name": tool.upstream_name,
                "description": tool.description,
                "input_schema": tool.schema,
                "schema_hash": tool.schema_hash,
                # Review #15: pinning stays optional; clients that care should send this hash.
                "schema_pin": "optional",
            }
        )

    # ---- invoke --------------------------------------------------------------------

    async def _invoke(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        server = await self._granted_server(key_id, args["server"])
        tool = await self._enabled_tool(server, args["tool"])
        pin = args.get("schema_hash")
        if pin is not None and pin != tool.schema_hash:
            raise PortunusError("schema_drift", "tool schema changed since schema_hash")
        arguments = args.get("arguments", {})
        _validate(tool.schema, arguments, what="tool arguments")
        timeout_s = self.invoke_timeout_s
        caller_shortened_timeout = False
        if "timeout_s" in args:
            requested = float(args["timeout_s"])
            timeout_s = min(requested, timeout_s)
            caller_shortened_timeout = requested < self.invoke_timeout_s

        if self.inflight(key_id) >= self.max_concurrent:
            raise PortunusError("rate_limited", f"over {self.max_concurrent} concurrent invokes")
        self._inflight[key_id] = self.inflight(key_id) + 1
        try:
            try:
                result = await self._run_invoke(key_id, server, tool, arguments, timeout_s)
            except (PortunusError, RegistryError) as e:
                await self.registry.record_outcome(
                    server.id,
                    e.code,
                    message=e.message,
                    caller_shortened_timeout=caller_shortened_timeout,
                )
                raise
            code = _error_code_from_result(result)
            await self.registry.record_outcome(
                server.id,
                code,
                message="",
                caller_shortened_timeout=caller_shortened_timeout,
            )
            return result
        finally:
            left = self.inflight(key_id) - 1
            if left > 0:
                self._inflight[key_id] = left
            else:
                self._inflight.pop(key_id, None)

    def _note_invoke(self, **fields: Any) -> None:
        bag = _invoke_attrs.get()
        if bag is not None:
            bag.update(fields)

    def _apply_rpc_timing(
        self, rpc_span: Any, timing: Any, *, fallback_ms: int
    ) -> None:
        """Copy runner ``timing`` onto invoke attrs + span; else use full rpc RTT as upstream_ms."""
        if isinstance(timing, dict):
            for key, attr in (
                ("queue_ms", tel.ATTR_QUEUE_MS),
                ("init_ms", tel.ATTR_INIT_MS),
                ("upstream_ms", tel.ATTR_UPSTREAM_MS),
            ):
                val = timing.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    ms = int(val)
                    self._note_invoke(**{key: ms})
                    rpc_span.set_attribute(attr, ms)
            return
        # Old runner / client-side timeout: omit queue/init.
        self._note_invoke(upstream_ms=fallback_ms)
        rpc_span.set_attribute(tel.ATTR_UPSTREAM_MS, fallback_ms)

    async def _run_invoke(
        self,
        key_id: str,
        server: ServerRow,
        tool: ToolRow,
        arguments: dict[str, Any],
        timeout_s: float,
    ) -> types.CallToolResult:
        self._note_invoke(server=server.slug, tool=tool.upstream_name)
        if server.kind == "remote":
            return await self._run_remote_invoke(key_id, server, tool, arguments, timeout_s)
        start = await self.registry.start_payload(server.id)
        self.state_cache.put(server.id, server.enabled, start["vault_version"])
        child_attrs = {
            tel.ATTR_SERVER: server.slug,
            tel.ATTR_TOOL: tool.upstream_name,
            tel.ATTR_KEY_ID: key_id,
        }
        t_start = time.perf_counter()
        with tel.start_span("portunus.start_child", attributes=child_attrs) as start_span:
            reserved = await self.slots.reserve_and_start(
                server.id, key_id, start, preferred_regions=start.get("preferred_regions") or ()
            )
            child_id = reserved.child_id
            cold_start = reserved.cold_start
            slot = self.slots.slot(child_id)
            preferred_miss = bool(slot.preferred_miss) if slot is not None else False
            site_id = slot.site_id if slot is not None else None
            start_span.set_attribute(tel.ATTR_COLD_START, cold_start)
            start_span.set_attribute(tel.ATTR_PREFERRED_MISS, preferred_miss)
            if site_id is not None:
                start_span.set_attribute(tel.ATTR_SITE_ID, site_id)
            if cold_start and preferred_miss:
                tel.record_preferred_miss(server=server.slug)
        start_ms = _ms_since(t_start)
        self._note_invoke(
            start_ms=start_ms,
            cold_start=cold_start,
            site_id=site_id,
            preferred_miss=preferred_miss,
        )
        if site_id is None:
            raise PortunusError("upstream_crash", "child exited before the call")
        rpc_attrs = {
            **child_attrs,
            tel.ATTR_SITE_ID: site_id,
        }
        t_rpc = time.perf_counter()
        with tel.start_span(
            "portunus.runner.rpc", kind=SpanKind.CLIENT, attributes=rpc_attrs
        ) as rpc_span:
            try:
                reply = await self.pool.request(
                    site_id,
                    "rpc",
                    timeout=timeout_s + RPC_SLACK_S,
                    child_id=child_id,
                    timeout_s=timeout_s,
                    payload={
                        "method": "tools/call",
                        "params": {"name": tool.upstream_name, "arguments": arguments},
                    },
                )
            except asyncio.CancelledError:
                self.slots.stop_pair(key_id, server.id)  # caller left mid-call: child may be wedged
                raise
            except RunnerRequestError as e:
                if e.code == "upstream_timeout":
                    self.slots.stop_pair(key_id, server.id)
                # Runner may still have attached timing on an error reply.
                err_timing = e.reply.get("timing") if isinstance(e.reply, dict) else None
                self._apply_rpc_timing(rpc_span, err_timing, fallback_ms=_ms_since(t_rpc))
                tel.set_span_error(rpc_span, e.code)
                if e.reason == "unknown_child":
                    raise PortunusError("upstream_crash", "child exited during the call") from None
                raise PortunusError(e.code, e.message) from None
            self._apply_rpc_timing(
                rpc_span,
                reply.get("timing") if isinstance(reply, dict) else None,
                fallback_ms=_ms_since(t_rpc),
            )

        # Authorization may have changed while the call ran: never hand back that result.
        self._check_key(key_id)
        if not self.authz.has_grant(key_id, server.id):
            raise PortunusError("not_granted", "grant removed during the call")
        slot = self.slots.slot(child_id)
        if slot is not None and slot.pending_stop:
            raise PortunusError("cancelled", "server stopped during the call")
        return finish_result(
            reply.get("result"), server=server.slug, tool=tool.upstream_name
        )

    async def _run_remote_invoke(
        self,
        key_id: str,
        server: ServerRow,
        tool: ToolRow,
        arguments: dict[str, Any],
        timeout_s: float,
    ) -> types.CallToolResult:
        proxy = self.registry.remote
        if proxy is None:
            raise PortunusError("unsupported", "remote proxy is not configured")
        self.state_cache.put(server.id, server.enabled, server.vault_version)
        remote_attrs = {
            tel.ATTR_SERVER: server.slug,
            tel.ATTR_TOOL: tool.upstream_name,
            tel.ATTR_KEY_ID: key_id,
        }
        t_up = time.perf_counter()
        with tel.start_span(
            "portunus.remote.invoke", kind=SpanKind.CLIENT, attributes=remote_attrs
        ) as remote_span:
            try:
                result = await proxy.invoke(
                    server.id, tool.upstream_name, arguments, timeout_s=timeout_s
                )
            except PortunusError as e:
                self._note_invoke(upstream_ms=_ms_since(t_up))
                remote_span.set_attribute(tel.ATTR_UPSTREAM_MS, _ms_since(t_up))
                tel.set_span_error(remote_span, e.code)
                if e.code == "auth_required":
                    return error_result(
                        "auth_required",
                        (
                            f"remote server requires authorization; "
                            f"reconnect '{server.slug}' in the Portunus Apps dashboard"
                        ),
                        data={"server": server.slug, "action": "reauth"},
                    )
                raise
            except RegistryError as e:
                self._note_invoke(upstream_ms=_ms_since(t_up))
                remote_span.set_attribute(tel.ATTR_UPSTREAM_MS, _ms_since(t_up))
                code = e.code if e.code in ERROR_CODES else "upstream_crash"
                tel.set_span_error(remote_span, code)
                raise PortunusError(code, e.message) from e
            self._note_invoke(upstream_ms=_ms_since(t_up))
            remote_span.set_attribute(tel.ATTR_UPSTREAM_MS, _ms_since(t_up))
        self._check_key(key_id)
        if not self.authz.has_grant(key_id, server.id):
            raise PortunusError("not_granted", "grant removed during the call")
        if not server.enabled:
            raise PortunusError("server_disabled", "server is disabled")
        return finish_result(result, server=server.slug, tool=tool.upstream_name)


# ---- ASGI gate -------------------------------------------------------------------------

# Application-defined JSON-RPC codes for HTTP guards (-32000..-32099). String Portunus codes
# stay in ``error.data.code`` so clients that already branch on them keep working.
_GUARD_RPC_CODES = {
    "unauthorized": -32001,
    "payload_too_large": -32002,
}
_JSONRPC_ADAPTER = TypeAdapter(types.JSONRPCMessage)


def _gate_trace_id() -> str:
    """Trace id for gate-layer errors (401/413/envelope); always creates a span context."""
    with tel.start_span("portunus.gate", kind=SpanKind.SERVER):
        return tel.current_trace_id()


def _error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    """JSON-RPC envelope for gate failures; Portunus string code lives in ``data.code``."""
    body = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": _GUARD_RPC_CODES.get(code, -32000),
            "message": message,
            "data": {"code": code, "trace_id": _gate_trace_id()},
        },
    }
    return JSONResponse(body, status, headers)


def _rpc_error_response(status: int, code: int, message: str):
    """A JSON-RPC level failure, shaped like the transport's own (e.g. -32700 on a parse error)."""
    body = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": code,
            "message": message,
            "data": {"trace_id": _gate_trace_id()},
        },
    }
    return JSONResponse(body, status)


def _envelope_invalid(body: bytes) -> bool:
    """True when the body is JSON but not a valid single JSON-RPC message."""
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False  # let the transport emit -32700
    if isinstance(raw, list):
        return False  # batch path already refused
    try:
        _JSONRPC_ADAPTER.validate_python(raw)
    except PydanticValidationError:
        return True
    return False


def _replay(body: bytes, receive: Receive) -> Receive:
    """A ``receive`` that serves the already-buffered body once, then defers to the real one."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class _Gate:
    """Authenticates the agent key, caps the body at 1MiB, refuses batches, then hands off."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        authorization = headers.get("authorization", "").strip()
        scheme, _, token = authorization.partition(" ")
        challenge = {"WWW-Authenticate": 'Bearer realm="portunus"'}
        if not authorization or (scheme.lower() == "bearer" and not token.strip()):
            problem = "agent key required"
        elif scheme.lower() != "bearer":
            # A bare ``Authorization: pm-...`` is the usual mistake; say what is wrong instead of
            # claiming no key was sent at all.
            problem = "Authorization must use the Bearer scheme"
        else:
            problem = ""
        if problem:
            await _error_response(401, "unauthorized", problem, challenge)(scope, receive, send)
            return
        try:
            row = await verify_agent_token(token.strip())
        except AuthError as e:
            await _error_response(401, e.code, e.message, challenge)(scope, receive, send)
            return

        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await _error_response(413, "payload_too_large", "request body exceeds 1MiB")(
                scope, receive, send
            )
            return
        body_receive = receive
        if scope["method"] == "POST":
            chunks: list[bytes] = []
            total = 0
            while True:
                msg = await receive()
                if msg["type"] != "http.request":
                    return  # client went away
                total += len(msg.get("body", b""))
                if total > MAX_BODY_BYTES:
                    await _error_response(413, "payload_too_large", "request body exceeds 1MiB")(
                        scope, receive, send
                    )
                    return
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
            body = b"".join(chunks)
            if body.lstrip()[:1] == b"[":
                # JSON-RPC batching was dropped in MCP 2025-06-18, but the transport still
                # negotiates 2024-11-05 for clients that ask, where a batch is legal. Refuse it
                # here: the transport's own rejection leaks a pydantic traceback and a docs URL.
                await _rpc_error_response(
                    400, -32600, "JSON-RPC batch is not supported; send one request per POST"
                )(scope, receive, send)
                return
            if _envelope_invalid(body):
                # Same leak class as the batch path: transport echoes a full pydantic union dump.
                await _rpc_error_response(400, -32602, "invalid JSON-RPC envelope")(
                    scope, receive, send
                )
                return
            body_receive = _replay(body, receive)

        scope = dict(scope)
        scope[KEY_SCOPE] = row.key_id
        await self._manager.handle_request(scope, body_receive, send)
