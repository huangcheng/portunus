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
* Errors reach the agent as ``isError`` tool results whose JSON text is
  ``{"error": {"code": <spec §6.4 code>, "message": ...}}``.
* Limits: 60 tool calls/min/key (``rate_limited``), 8 concurrent invokes/key (``rate_limited``),
  invoke timeout default 60s (an agent may only shorten it). A timed-out or cancelled call retires
  its child (it may be wedged mid-call).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

import mcp_types as types
from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from portunus_shared import DEFAULTS, ERROR_CODES, PortunusError
from sqlalchemy import select
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from .db.models import Server as ServerRow
from .db.models import Tool as ToolRow
from .keys import AuthError, verify_agent_token
from .registry import RegistryError
from .runner_client import RunnerRequestError
from .runner_pool import RunnerPool

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
                "description": "Optional: refuse unless the stored schema still has this hash.",
            },
            "timeout_s": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Optional: shorten (never extend) the invoke timeout.",
            },
        },
        ["server", "tool"],
    ),
}
_DESCRIPTIONS = {
    "portunus_discover_servers": "List the MCP servers this key may use (paged).",
    "portunus_discover_tools": "List the enabled tools of a granted server (paged).",
    "portunus_get_tool_schema": "Get the input schema and description of one tool.",
    "portunus_invoke_tool": "Invoke one tool of a granted server with arguments.",
}


def list_tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(name=n, description=_DESCRIPTIONS[n], input_schema=_SCHEMAS[n]) for n in TOOL_NAMES
    ]


# ---- results ---------------------------------------------------------------------------


def error_result(code: str, message: str = "") -> types.CallToolResult:
    if code not in ERROR_CODES:
        code, message = "upstream_crash", message or "internal error"
    body = {"error": {"code": code, "message": message}}
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(body))], structured_content=body, is_error=True
    )


def ok_result(obj: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(obj))], structured_content=obj
    )


def finish_result(result: Any) -> types.CallToolResult:
    """Turn an upstream ``tools/call`` result into the agent-facing result.

    Over 1MiB serialized -> ``payload_too_large`` (the payload is dropped entirely: an agent must
    never mistake a truncated payload for a successful one).
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
    return types.CallToolResult(
        content=parsed.content, structured_content=parsed.structured_content, is_error=parsed.is_error
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
        raise PortunusError("unsupported", "invalid cursor") from None
    if not isinstance(after, str):
        raise PortunusError("unsupported", "invalid cursor")
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
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        cls(schema).validate(value)
    except ValidationError as e:
        raise PortunusError("schema_mismatch", f"{what}: {_first_error(e)}") from None
    except SchemaError:
        raise PortunusError("schema_mismatch", f"{what}: stored schema is not valid") from None


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
        if not isinstance(key_id, str):
            return error_result("unauthorized", "no authenticated agent key")
        return await self.dispatch(key_id, params.name, params.arguments or {})

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

    async def dispatch(self, key_id: str, name: str, args: dict[str, Any]) -> types.CallToolResult:
        """Run one meta tool for an authenticated key. Never raises for expected failures."""
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
            return await handler(key_id, args)
        except PortunusError as e:
            return error_result(e.code, e.message)
        except RegistryError as e:
            return error_result(e.code if e.code in ERROR_CODES else "spawn_failed", e.message)

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
        if server.kind != "stdio":
            raise PortunusError("unsupported", f"server kind {server.kind!r} is not supported yet")
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
            raise PortunusError("tool_disabled", "tool is not enabled")
        return tool

    # ---- discovery -----------------------------------------------------------------

    @staticmethod
    def _paging(args: dict[str, Any]) -> tuple[str, str | None, int]:
        return str(args.get("query", "")).casefold(), args.get("cursor"), int(args.get("limit", PAGE_DEFAULT))

    async def _discover_servers(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        query, cursor, limit = self._paging(args)
        async with self._sf() as s:
            servers = (
                (await s.execute(select(ServerRow).where(ServerRow.enabled.is_(True))))
                .scalars()
                .all()
            )
            counts: dict[uuid.UUID, int] = {}
            for sid in (
                await s.execute(select(ToolRow.server_id).where(ToolRow.enabled.is_(True)))
            ).scalars():
                counts[sid] = counts.get(sid, 0) + 1
        items = [
            {"slug": sv.slug, "name": sv.name, "kind": sv.kind, "tool_count": counts.get(sv.id, 0)}
            for sv in sorted(servers, key=lambda x: x.slug)
            if self.authz.has_grant(key_id, sv.id)
            and (not query or query in sv.slug.casefold() or query in sv.name.casefold())
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
            }
        )

    # ---- invoke --------------------------------------------------------------------

    async def _invoke(self, key_id: str, args: dict[str, Any]) -> types.CallToolResult:
        server = await self._granted_server(key_id, args["server"])
        tool = await self._enabled_tool(server, args["tool"])
        pin = args.get("schema_hash")
        if pin is not None and pin != tool.schema_hash:
            raise PortunusError("schema_mismatch", "tool schema changed since schema_hash")
        arguments = args.get("arguments", {})
        _validate(tool.schema, arguments, what="tool arguments")
        timeout_s = self.invoke_timeout_s
        if "timeout_s" in args:
            timeout_s = min(float(args["timeout_s"]), timeout_s)

        if self.inflight(key_id) >= self.max_concurrent:
            raise PortunusError("rate_limited", f"over {self.max_concurrent} concurrent invokes")
        self._inflight[key_id] = self.inflight(key_id) + 1
        try:
            return await self._run_invoke(key_id, server, tool, arguments, timeout_s)
        finally:
            left = self.inflight(key_id) - 1
            if left > 0:
                self._inflight[key_id] = left
            else:
                self._inflight.pop(key_id, None)

    async def _run_invoke(
        self,
        key_id: str,
        server: ServerRow,
        tool: ToolRow,
        arguments: dict[str, Any],
        timeout_s: float,
    ) -> types.CallToolResult:
        start = await self.registry.start_payload(server.id)
        self.state_cache.put(server.id, server.enabled, start["vault_version"])
        child_id = await self.slots.reserve_and_start(
            server.id, key_id, start, preferred_regions=start.get("preferred_regions") or ()
        )
        site_id = self.slots.site_of(child_id)  # the rpc goes to the site that owns the child
        if site_id is None:
            raise PortunusError("upstream_crash", "child exited before the call")
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
            if e.reason == "unknown_child":
                raise PortunusError("upstream_crash", "child exited during the call") from None
            raise PortunusError(e.code, e.message) from None

        # Authorization may have changed while the call ran: never hand back that result.
        self._check_key(key_id)
        if not self.authz.has_grant(key_id, server.id):
            raise PortunusError("not_granted", "grant removed during the call")
        slot = self.slots.slot(child_id)
        if slot is not None and slot.pending_stop:
            raise PortunusError("cancelled", "server stopped during the call")
        return finish_result(reply.get("result"))


# ---- ASGI gate -------------------------------------------------------------------------


def _error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers)


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
    """Authenticates the agent key, caps the body at 1MiB, then hands off to the MCP transport."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        challenge = {"WWW-Authenticate": 'Bearer realm="portunus"'}
        if scheme.lower() != "bearer" or not token.strip():
            await _error_response(401, "unauthorized", "agent key required", challenge)(
                scope, receive, send
            )
            return
        try:
            row = await verify_agent_token(token.strip())
        except AuthError as e:
            await _error_response(401, e.code, e.message, challenge)(scope, receive, send)
            return

        declared = headers.get("content-length", "")
        too_big = _error_response(413, "payload_too_large", "request body exceeds 1MiB")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await too_big(scope, receive, send)
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
                    await too_big(scope, receive, send)
                    return
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
            body_receive = _replay(b"".join(chunks), receive)

        scope = dict(scope)
        scope[KEY_SCOPE] = row.key_id
        await self._manager.handle_request(scope, body_receive, send)
