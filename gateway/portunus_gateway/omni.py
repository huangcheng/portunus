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
* A tool result over 1MiB (serialized) becomes ``payload_too_large`` by default. Callers may pass
  ``max_bytes`` / ``truncate=true`` to get an explicit truncated preview instead. The runner
  enforces its own larger bound (and kills the child).
* A result is only handed back if the key is still authorized and the child was not marked
  ``pending_stop`` while the call ran (revoke / ungrant / disable / vault bump mid-call).
* A request body that is a JSON-RPC batch is refused with ``-32600`` before it reaches the
  transport, which would otherwise leak a pydantic traceback instead of a protocol error.
  Other envelope validation failures are refused here with ``-32602`` for the same reason.
* A modern (2026-07-28+) envelope whose ``MCP-Protocol-Version`` header is missing or
  disagrees with ``params._meta`` is refused here with ``400`` + ``HeaderMismatch``; the SDK
  routes eras by the header alone and would otherwise serve the request under legacy rules.
* HTTP-layer guard errors use application JSON-RPC codes below the JSON-RPC reserved range
  (MCP 2026-07-28 forbids new codes in ``-32000..-32099``; ``-32002`` must never be emitted).
* HTTP-layer guards (auth, body size) use the same JSON-RPC error envelope as the transport,
  with the Portunus string code preserved in ``error.data.code``.
* ``serverInfo.version`` is the package version, never blank.
* Errors reach the agent as ``isError`` tool results whose JSON text is
  ``{"error": {"code": <spec §6.4 code>, "message": ...}}``.
* When an upstream result has no ``structuredContent`` but ``content[0].text`` is JSON, it is
  promoted so remote and stdio results share one shape.
* Limits: 60 ``portunus_invoke_tool`` calls/min/key (``rate_limited``; discovery / schema
  reads are free), 8 concurrent invokes/key (``rate_limited``), invoke timeout default 60s
  (an agent may only shorten it). A timed-out or cancelled call retires its child (it may be
  wedged mid-call). ``rate_limited`` includes ``retry_after_s`` in ``error.data`` and
  ``_meta.io.portunus.gateway``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
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
from .config import load_pepper
from .keys import AuthError, verify_agent_token
from .origin import origin_allowed
from .protected_resource import bearer_challenge_headers
from .registry import RegistryError, effective_schema
from .runner_client import RunnerRequestError
from .runner_pool import RunnerPool

# One automatic retry after a transient upstream timeout (read-only / idempotent only).
_INVOKE_TIMEOUT_RETRY_DELAY_S = 0.3
_SAFE_WEBDAV_RETRY_TOOLS = frozenset(
    {
        "webdav_list_remote_directory",
        "webdav_stat",
        "webdav_read_resource",
        "webdav_get_file",
    }
)


def _timeout_retryable(tool: ToolRow) -> bool:
    """Only retry when a duplicate call cannot create a second side effect."""
    ann = tool.annotations if isinstance(tool.annotations, dict) else {}
    if ann.get("readOnlyHint") is True or ann.get("idempotentHint") is True:
        return True
    name = (tool.upstream_name or "").casefold()
    return name in _SAFE_WEBDAV_RETRY_TOOLS

# Prefixed _meta key (MCP naming MUST). Legacy ``portunus`` is dual-written for one release.
META_PORTUNUS = "io.portunus.gateway"

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
                    "Optional pin from portunus_get_tool_schema / discover_tools. When set, a "
                    "changed schema returns schema_drift. When omitted, the invoke still runs "
                    "and _meta marks schema_unpinned=true (soft-warn; pin is not required)."
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
            "max_bytes": {
                "type": "integer",
                "minimum": 1024,
                "maximum": MAX_RESULT_BYTES,
                "description": (
                    f"Soft size cap for this invoke (default {MAX_RESULT_BYTES}). "
                    "Over the cap: payload_too_large unless truncate=true."
                ),
            },
            "truncate": {
                "type": "boolean",
                "description": (
                    "When true and the result exceeds max_bytes, return a truncated preview "
                    "with truncated:true instead of payload_too_large."
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
        "successful probe or live invoke), last_probe_at / last_ok_at / last_error_* "
        "timestamps, token_expires_at (UTC ISO when the access grant's lifetime is known; "
        "null if unknown or non-OAuth), and has_refresh_token (true when a refresh_token is "
        "vaulted so renew is possible without re-auth). An unreachable status reflects the "
        "last observation; a retry is allowed."
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
    """Merge gateway attribution into ``result.meta`` (wire alias ``_meta``), never structuredContent.

    Writes the prefixed key ``io.portunus.gateway`` (spec naming) and dual-writes legacy
    ``portunus`` for one release so existing clients keep working.
    """
    meta = dict(result.meta) if result.meta else {}
    existing = meta.get(META_PORTUNUS)
    if not isinstance(existing, dict):
        existing = meta.get("portunus")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(portunus)
    meta[META_PORTUNUS] = merged
    meta["portunus"] = merged  # legacy alias
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


def _unwrap_cli_payload(structured: Any) -> dict[str, Any] | None:
    """Best-effort unwrap of skill-bridge / CLI envelopes (``result`` string or object).

    json_cli envelopes are ``{exit_code, result, stderr}``. Keep ``exit_code`` visible for
    Quark-style failures, but surface a *failure-shaped* nested ``result`` (LOGIN_PENDING,
    ok=false, logged_in=false) so ``_cli_business_failure`` still classifies them.
    """
    if not isinstance(structured, dict):
        return None

    inner = structured.get("result")
    parsed_inner: Any = inner
    if isinstance(inner, str) and inner.strip()[:1] in "{[":
        try:
            parsed_inner = json.loads(inner)
        except (json.JSONDecodeError, TypeError):
            parsed_inner = None
    if isinstance(parsed_inner, dict) and (
        parsed_inner.get("code") in ("LOGIN_PENDING", "LOGIN_REQUIRED")
        or parsed_inner.get("ok") is False
        or parsed_inner.get("logged_in") is False
    ):
        # Prefer nested business fields; keep envelope stderr/message/exit_code.
        merged = {**structured, **parsed_inner}
        if "exit_code" in structured:
            merged["exit_code"] = structured["exit_code"]
        return merged

    if (
        "exit_code" in structured
        or "ok" in structured
        or structured.get("code") in ("LOGIN_PENDING", "LOGIN_REQUIRED")
    ):
        return structured

    if isinstance(parsed_inner, dict):
        return parsed_inner
    return structured


def _cli_business_failure(
    structured: Any,
) -> tuple[str, str, dict[str, Any]] | None:
    """Classify CLI/business failures that arrive with ``isError: false`` (review v7 §4).

    Returns ``(code, message, upstream_detail)`` or ``None`` when the payload looks successful.
    """
    payload = _unwrap_cli_payload(structured)
    if payload is None:
        return None
    detail = {k: payload[k] for k in ("code", "exit_code", "ok", "errcode", "logged_in") if k in payload}

    def _msg(*candidates: Any, fallback: str) -> str:
        for c in candidates:
            if isinstance(c, str) and c.strip():
                return c.strip()[:240]
            if isinstance(c, dict):
                for key in ("message", "msg", "errmsg", "error"):
                    v = c.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:240]
        return fallback[:240]

    nested = payload.get("result") if isinstance(payload.get("result"), dict) else None
    # Auth signals win over a non-zero CLI exit_code (Guangyapan LOGIN_REQUIRED
    # often arrives as exit_code=1 + nested code — do not mislabel as upstream_error).
    code = payload.get("code")
    if code in ("LOGIN_PENDING", "LOGIN_REQUIRED") or (
        payload.get("logged_in") is False and code is not None
    ):
        msg = _msg(
            payload.get("message"),
            nested,
            payload.get("data") if isinstance(payload.get("data"), dict) else None,
            fallback="upstream login required",
        )
        return "auth_required", msg, detail

    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        if isinstance(nested, dict) and "code" in nested and "code" not in detail:
            detail["code"] = nested["code"]
        msg = _msg(
            payload.get("message"),
            payload.get("stderr"),
            payload.get("error"),
            nested,
            fallback=f"upstream CLI exited with code {exit_code}",
        )
        return "upstream_error", msg, detail

    if payload.get("ok") is False:
        msg = _msg(
            payload.get("message"),
            payload.get("errmsg"),
            payload.get("error"),
            nested,
            fallback=f"upstream reported ok=false (errcode={payload.get('errcode')})",
        )
        return "upstream_error", msg, detail
    return None


def finish_result(
    result: Any,
    *,
    server: str | None = None,
    tool: str | None = None,
    max_bytes: int = MAX_RESULT_BYTES,
    truncate: bool = False,
) -> types.CallToolResult:
    """Turn an upstream ``tools/call`` result into the agent-facing result.

    Over ``max_bytes`` (hard ceiling ``MAX_RESULT_BYTES``): ``payload_too_large`` unless
    ``truncate=True``, which returns a preview with ``truncated: true`` (review #25).

    Proxied ``isError`` results without a gateway envelope become ``upstream_error``
    (review #16 / #19-failure) while keeping the original ``content[0].text``.

    CLI envelopes with ``exit_code != 0``, ``ok: false``, or ``LOGIN_PENDING`` become
    ``isError`` even when the transport reported success (review v7 §4 / #16-residual).
    """
    if not isinstance(result, dict):
        return error_result("upstream_crash", "malformed tool result")
    cap = max(1024, min(int(max_bytes), MAX_RESULT_BYTES))
    try:
        parsed = types.CallToolResult.model_validate(result)
    except Exception:
        return error_result("upstream_crash", "malformed tool result")
    structured = _promote_structured(list(parsed.content), parsed.structured_content)
    upstream: dict[str, Any] = {}
    if server:
        upstream["server"] = server
    if tool:
        upstream["tool"] = tool

    # Classify failures before size gates so QR-heavy LOGIN_PENDING (etc.) cannot
    # be misreported as a truncated success when truncate=True. Oversized errors
    # still hit the byte ceiling below (is_error preserved).
    classified: types.CallToolResult | None = None
    if parsed.is_error and not _already_gateway_error(structured):
        text = _first_text(list(parsed.content))
        message = (text.strip().split("\n", 1)[0] if text.strip() else "")[:240] or (
            "upstream tool reported an error"
        )
        body = {
            "error": {
                "code": "upstream_error",
                "message": message,
                **({"upstream": upstream} if upstream else {}),
            }
        }
        classified = types.CallToolResult(
            content=parsed.content, structured_content=body, is_error=True
        )
    elif not parsed.is_error and not _already_gateway_error(structured):
        failure = _cli_business_failure(structured)
        if failure is not None:
            code, message, detail = failure
            body = {
                "error": {
                    "code": code,
                    "message": message,
                    **({"upstream": {**upstream, **detail}} if upstream or detail else {}),
                }
            }
            # Keep the original content so operators can still inspect the CLI payload
            # when it fits; oversized paths replace content with the compact envelope.
            classified = types.CallToolResult(
                content=parsed.content, structured_content=body, is_error=True
            )

    raw = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()
    size = len(raw)
    if size > cap:
        if not truncate:
            return error_result(
                "payload_too_large",
                f"tool result exceeds the {cap} byte limit ({size} bytes)",
                data={"bytes": size, "limit": cap},
            )
        if classified is not None:
            # Classified error wins over a truncated "success" preview.
            err_body = classified.structured_content
            compact = json.dumps(err_body, ensure_ascii=False, separators=(",", ":"))
            return types.CallToolResult(
                content=[types.TextContent(text=compact)],
                structured_content=err_body,
                is_error=True,
            )
        preview = raw[: min(cap, 64 * 1024)].decode("utf-8", errors="replace")
        body = {
            "truncated": True,
            "bytes": size,
            "limit": cap,
            "preview": preview,
        }
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(body, ensure_ascii=False))],
            structured_content=body,
            # Preserve already-gateway / transport isError (never flip to success).
            is_error=bool(parsed.is_error),
        )

    if classified is not None:
        return classified
    return types.CallToolResult(
        content=parsed.content, structured_content=structured, is_error=parsed.is_error
    )


# ---- paging ----------------------------------------------------------------------------


def encode_cursor(after: str, *, scope: str, query: str = "") -> str:
    """Signed keyset cursor bound to ``scope`` + ``query`` (review #26)."""
    payload = {"after": after, "scope": scope, "query": query}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(load_pepper(), body, hashlib.sha256).hexdigest()[:32]
    blob = {"after": after, "scope": scope, "query": query, "sig": sig}
    return base64.urlsafe_b64encode(json.dumps(blob, separators=(",", ":")).encode()).decode().rstrip(
        "="
    )


def decode_cursor(
    cursor: str | None, *, scope: str, query: str = ""
) -> str | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        blob = json.loads(raw)
        after = blob["after"]
        got_scope = blob["scope"]
        got_query = blob.get("query", "")
        got_sig = blob["sig"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise PortunusError("invalid_cursor", "invalid cursor") from None
    if not isinstance(after, str) or not isinstance(got_sig, str):
        raise PortunusError("invalid_cursor", "invalid cursor")
    if got_scope != scope or got_query != query:
        raise PortunusError("invalid_cursor", "cursor does not match this query")
    expect = {"after": after, "scope": got_scope, "query": got_query}
    body = json.dumps(expect, separators=(",", ":"), sort_keys=True).encode()
    want = hmac.new(load_pepper(), body, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(got_sig, want):
        raise PortunusError("invalid_cursor", "invalid cursor")
    return after


def _page(
    items: list[dict[str, Any]],
    key: str,
    cursor: str | None,
    limit: int,
    *,
    scope: str,
    query: str = "",
) -> dict[str, Any]:
    """Keyset page over ``items`` sorted by ``item[key]``."""
    after = decode_cursor(cursor, scope=scope, query=query)
    rest = [i for i in items if after is None or i[key] > after]
    page = rest[:limit]
    more = len(rest) > limit
    return {
        "items": page,
        "next_cursor": encode_cursor(page[-1][key], scope=scope, query=query) if more else None,
    }


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

    def _rate_limit_invoke(self, key_id: str) -> None:
        """Charge one invoke against the per-key sliding window (discovery is free)."""
        now = self._clock()
        window = self._calls.setdefault(key_id, deque())
        while window and window[0] <= now - 60.0:
            window.popleft()
        if len(window) >= self.rate_per_min:
            oldest = window[0] if window else now
            retry_after_s = max(1, int(60.0 - (now - oldest)) + 1)
            raise PortunusError(
                "rate_limited",
                f"over {self.rate_per_min} invokes per minute",
                data={"retry_after_s": retry_after_s},
            )
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
                        # Discovery / schema reads are cheap and cacheable; only invokes
                        # burn the 60/min budget (F7).
                        if name == "portunus_invoke_tool":
                            self._rate_limit_invoke(key_id)
                        handler = {
                            "portunus_discover_servers": self._discover_servers,
                            "portunus_discover_tools": self._discover_tools,
                            "portunus_get_tool_schema": self._get_tool_schema,
                            "portunus_invoke_tool": self._invoke,
                        }[name]
                        result = await handler(key_id, args)
                    except PortunusError as e:
                        tel.set_span_error(span, e.code)
                        result = error_result(e.code, e.message, data=e.data)
                        if e.code == "rate_limited" and isinstance(e.data, dict):
                            retry = e.data.get("retry_after_s")
                            if isinstance(retry, int):
                                invoke_attrs["retry_after_s"] = retry
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
                        "retry_after_s",
                        "schema_unpinned",
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
        return ok_result(_page(items, "slug", cursor, limit, scope="servers", query=query))

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
        items: list[dict[str, Any]] = []
        for t in sorted(tools, key=lambda x: x.upstream_name):
            if query and query not in t.upstream_name.casefold() and query not in (
                t.description or ""
            ).casefold():
                continue
            row: dict[str, Any] = {
                "name": t.upstream_name,
                "description": t.description,
                "schema_hash": t.schema_hash,
                # Review #31: inline schema so agents need fewer round trips.
                "input_schema": effective_schema(t),
            }
            if t.annotations:
                row["annotations"] = t.annotations
            items.append(row)
        return ok_result(
            _page(items, "name", cursor, limit, scope=f"tools:{server.slug}", query=query)
        )

    async def _get_tool_schema(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        server = await self._granted_server(key_id, args["server"])
        tool = await self._enabled_tool(server, args["tool"])
        out: dict[str, Any] = {
            "server": server.slug,
            "name": tool.upstream_name,
            "description": tool.description,
            "input_schema": effective_schema(tool),
            "schema_hash": tool.schema_hash,
            # Soft-warn policy: pin is optional; unpinned invokes set _meta.schema_unpinned.
            "schema_pin": tool.schema_hash,
            "schema_pin_required": False,
        }
        if tool.annotations:
            out["annotations"] = tool.annotations
        return ok_result(out)

    # ---- invoke --------------------------------------------------------------------

    async def _invoke(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        server = await self._granted_server(key_id, args["server"])
        tool = await self._enabled_tool(server, args["tool"])
        pin = args.get("schema_hash")
        if pin is not None and pin != tool.schema_hash:
            raise PortunusError("schema_drift", "tool schema changed since schema_hash")
        # Soft-warn (option B): unpinned invokes are allowed; surface observability in _meta.
        if pin is None:
            bag = _invoke_attrs.get()
            if bag is not None:
                bag["schema_unpinned"] = True
        arguments = args.get("arguments", {})
        _validate(effective_schema(tool), arguments, what="tool arguments")
        timeout_s = self.invoke_timeout_s
        caller_shortened_timeout = False
        if "timeout_s" in args:
            requested = float(args["timeout_s"])
            timeout_s = min(requested, timeout_s)
            caller_shortened_timeout = requested < self.invoke_timeout_s

        max_bytes = int(args.get("max_bytes", MAX_RESULT_BYTES))
        truncate = bool(args.get("truncate", False))
        if self.inflight(key_id) >= self.max_concurrent:
            raise PortunusError(
                "rate_limited",
                f"over {self.max_concurrent} concurrent invokes",
                data={"retry_after_s": 1},
            )
        self._inflight[key_id] = self.inflight(key_id) + 1
        try:
            try:
                result = await self._run_invoke(
                    key_id,
                    server,
                    tool,
                    arguments,
                    timeout_s,
                    max_bytes=max_bytes,
                    truncate=truncate,
                )
            except (PortunusError, RegistryError) as e:
                # One retry for transient timeouts on read-only/idempotent tools only.
                can_retry = (
                    e.code == "upstream_timeout"
                    and not caller_shortened_timeout
                    and _timeout_retryable(tool)
                )
                if can_retry:
                    # Record the first timeout so site health / last_error stay honest.
                    await self.registry.record_outcome(
                        server.id,
                        e.code,
                        message=e.message,
                        caller_shortened_timeout=caller_shortened_timeout,
                        site_id=self._invoke_site() or getattr(e, "site_id", None),
                    )
                    # Drop first-attempt timing/site so a failed retry is not attributed
                    # to a site that never ran the second call.
                    bag = _invoke_attrs.get()
                    if isinstance(bag, dict):
                        for key in (
                            "site_id",
                            "start_ms",
                            "cold_start",
                            "preferred_miss",
                            "queue_ms",
                            "init_ms",
                            "upstream_ms",
                        ):
                            bag.pop(key, None)
                    await asyncio.sleep(_INVOKE_TIMEOUT_RETRY_DELAY_S)
                    try:
                        result = await self._run_invoke(
                            key_id,
                            server,
                            tool,
                            arguments,
                            timeout_s,
                            max_bytes=max_bytes,
                            truncate=truncate,
                        )
                    except (PortunusError, RegistryError) as e2:
                        await self.registry.record_outcome(
                            server.id,
                            e2.code,
                            message=e2.message,
                            caller_shortened_timeout=caller_shortened_timeout,
                            site_id=self._invoke_site() or getattr(e2, "site_id", None),
                        )
                        raise
                else:
                    await self.registry.record_outcome(
                        server.id,
                        e.code,
                        message=e.message,
                        caller_shortened_timeout=caller_shortened_timeout,
                        # Cold-start failures raise before the dispatch bag knows the site;
                        # SlotManager attaches it to the error (slot.site_id).
                        site_id=self._invoke_site() or getattr(e, "site_id", None),
                    )
                    # Failover needs no extra push here: upstream_timeout already retires the
                    # pair in _run_invoke and link loss reconciles via reconnect; the learned
                    # streak recorded above is what moves the *next* placement.
                    raise
            code = _error_code_from_result(result)
            bag = _invoke_attrs.get() or {}
            up = bag.get("upstream_ms")
            await self.registry.record_outcome(
                server.id,
                code,
                message="",
                caller_shortened_timeout=caller_shortened_timeout,
                site_id=self._invoke_site(),
                upstream_ms=up if isinstance(up, int) else None,
            )
            return result
        finally:
            left = self.inflight(key_id) - 1
            if left > 0:
                self._inflight[key_id] = left
            else:
                self._inflight.pop(key_id, None)

    @staticmethod
    def _invoke_site() -> str | None:
        """The hosting runner site for the in-flight invoke, read from the dispatch bag.

        Read fresh (never a snapshot taken before the run): the bag dict is mutated in
        place by ``_note_invoke`` once the slot reservation names the site.
        """
        bag = _invoke_attrs.get()
        site = bag.get("site_id") if isinstance(bag, dict) else None
        return site if isinstance(site, str) else None

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
        *,
        max_bytes: int = MAX_RESULT_BYTES,
        truncate: bool = False,
    ) -> types.CallToolResult:
        self._note_invoke(server=server.slug, tool=tool.upstream_name)
        if server.kind == "remote":
            return await self._run_remote_invoke(
                key_id,
                server,
                tool,
                arguments,
                timeout_s,
                max_bytes=max_bytes,
                truncate=truncate,
            )
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
            reply.get("result"),
            server=server.slug,
            tool=tool.upstream_name,
            max_bytes=max_bytes,
            truncate=truncate,
        )

    async def _run_remote_invoke(
        self,
        key_id: str,
        server: ServerRow,
        tool: ToolRow,
        arguments: dict[str, Any],
        timeout_s: float,
        *,
        max_bytes: int = MAX_RESULT_BYTES,
        truncate: bool = False,
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
                    server.id,
                    tool.upstream_name,
                    arguments,
                    timeout_s=timeout_s,
                    # Effective (operator override or probed) schema drives Mcp-Param-* mirroring.
                    input_schema=effective_schema(tool),
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
        return finish_result(
            result,
            server=server.slug,
            tool=tool.upstream_name,
            max_bytes=max_bytes,
            truncate=truncate,
        )


# ---- ASGI gate -------------------------------------------------------------------------

# Application-defined JSON-RPC codes for HTTP guards. MCP 2026-07-28 partitions -32000..-32019
# (legacy, no new allocations; -32002 must never be emitted) and -32020..-32099 (spec-reserved),
# so app codes live below the JSON-RPC reserved range (-32768..-32000). String Portunus codes
# stay in ``error.data.code`` so clients that already branch on them keep working.
_GUARD_RPC_CODES = {
    "unauthorized": -40001,
    "payload_too_large": -40002,
    "forbidden": -40003,
}
_GUARD_RPC_DEFAULT = -40000
# Ranges no application code may occupy: JSON-RPC reserved (-32768..-32000) and the MCP
# spec-reserved block (-32020..-32099, which subsumes legacy -32000..-32019).
_GUARD_CODES_RESERVED_RANGES = ((-32768, -32000), (-32099, -32020))
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
            "code": _GUARD_RPC_CODES.get(code, _GUARD_RPC_DEFAULT),
            "message": message,
            "data": {"code": code, "trace_id": _gate_trace_id()},
        },
    }
    return JSONResponse(body, status, headers)


def _rpc_error_response(status: int, code: int, message: str, request_id: Any = None):
    """A JSON-RPC level failure, shaped like the transport's own (e.g. -32700 on a parse error)."""
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
            "data": {"trace_id": _gate_trace_id()},
        },
    }
    return JSONResponse(body, status)


_UNPARSABLE = object()  # distinct from JSON ``null`` so a null body still fails validation


def _decode_body(body: bytes) -> Any:
    """Decode the POST body once for every gate check.

    Returns :data:`_UNPARSABLE` on failure so the transport still owns the ``-32700``
    reply; JSON ``null`` (which decodes to ``None``) stays distinct and is rejected as
    an invalid envelope here instead of leaking a pydantic traceback from the transport.
    """
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return _UNPARSABLE


def _envelope_invalid(raw: Any) -> bool:
    """True when the body is JSON but not a valid single JSON-RPC message."""
    if raw is _UNPARSABLE:
        return False  # transport emits -32700
    if raw is None or isinstance(raw, list):
        # JSON null is not a message; a list is a batch the byte-level check may miss (BOM).
        return True
    try:
        _JSONRPC_ADAPTER.validate_python(raw)
    except PydanticValidationError:
        return True
    return False


# ``params._meta`` key that carries the modern (2026-07-28+) per-request protocol version.
_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"


def _protocol_envelope(raw: Any) -> tuple[str | None, str | int | None]:
    """``(meta protocol version, request id)`` from a decoded POST body, if parseable.

    Legacy (initialize-era) envelopes carry no ``params._meta`` version and return
    ``(None, id)``, which makes the gate's header cross-check a no-op for them.
    """
    if raw is _UNPARSABLE or not isinstance(raw, dict):
        return None, None
    rid = raw.get("id")
    if isinstance(rid, bool):
        rid = None
    elif isinstance(rid, float):
        # Mirror _rewrite_float_jsonrpc_id: 2.0 answers as 2 so the mismatch reply is correlatable.
        rid = int(rid) if rid.is_integer() else None
    elif not isinstance(rid, (str, int)):
        rid = None
    params = raw.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = meta.get(_META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    if not isinstance(version, str) or not version:
        version = None
    return version, rid


def _rewrite_float_jsonrpc_id(raw: Any) -> tuple[bytes | None, str | None]:
    """Coerce integer-valued float ``id`` to int; reject non-integer floats.

    The MCP SDK classifies float ``id`` as a notification (no response). JSON-RPC
    allows numeric ids; a request with ``id`` MUST get a response (compliance §4).
    Takes the already-decoded body; returns ``(rewritten_body_or_None, error_message_or_None)``.
    """
    if not isinstance(raw, dict) or "id" not in raw:
        return None, None
    rid = raw["id"]
    if isinstance(rid, bool) or not isinstance(rid, float):
        return None, None
    if rid.is_integer():
        raw["id"] = int(rid)
        return json.dumps(raw, separators=(",", ":"), ensure_ascii=False).encode(), None
    return None, "JSON-RPC id must be a string or integer (not a fractional number)"


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
        # MCP 2026-07-28 Streamable HTTP: Origin present + invalid → 403 (DNS rebinding).
        if not origin_allowed(headers.get("origin")):
            await _error_response(403, "forbidden", "invalid Origin")(scope, receive, send)
            return
        authorization = headers.get("authorization", "").strip()
        scheme, _, token = authorization.partition(" ")
        challenge = bearer_challenge_headers(headers)
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
            if body[:3] == b"\xef\xbb\xbf":
                body = body[3:]  # a BOM would hide a batch from the byte-level check below
            if body.lstrip()[:1] == b"[":
                # JSON-RPC batching was dropped in MCP 2025-06-18, but the transport still
                # negotiates 2024-11-05 for clients that ask, where a batch is legal. Refuse it
                # here: the transport's own rejection leaks a pydantic traceback and a docs URL.
                await _rpc_error_response(
                    400, -32600, "JSON-RPC batch is not supported; send one request per POST"
                )(scope, receive, send)
                return
            decoded = _decode_body(body)
            meta_version, request_id = _protocol_envelope(decoded)
            if meta_version is not None and headers.get("mcp-protocol-version") != meta_version:
                # MCP 2026-07-28: the header MUST equal ``params._meta`` protocolVersion; a
                # missing or disagreeing header is a 400 HeaderMismatch (-32020). The SDK
                # era-routes on the header alone and the legacy path never compares the two,
                # so a legacy-version header over a modern envelope would otherwise be served
                # (audit 2026-11, probe case C).
                await _rpc_error_response(
                    400,
                    types.HEADER_MISMATCH,
                    (
                        "mcp-protocol-version header does not match the request "
                        "envelope's protocol version"
                    ),
                    request_id=request_id,
                )(scope, receive, send)
                return
            rewritten, id_err = _rewrite_float_jsonrpc_id(decoded)
            if id_err:
                await _rpc_error_response(400, -32600, id_err)(scope, receive, send)
                return
            if rewritten is not None:
                body = rewritten
            if _envelope_invalid(decoded):
                # Same leak class as the batch path: transport echoes a full pydantic union dump.
                await _rpc_error_response(400, -32602, "invalid JSON-RPC envelope")(
                    scope, receive, send
                )
                return
            body_receive = _replay(body, receive)

        scope = dict(scope)
        scope[KEY_SCOPE] = row.key_id
        await self._manager.handle_request(scope, body_receive, send)
