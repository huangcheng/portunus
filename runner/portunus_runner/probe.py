"""Ephemeral MCP ``tools/list`` probe over a freshly spawned stdio child (spec §5, §6.2).

A probe is dashboard/scheduler-only: the child lives in the ``probe`` namespace -- outside the
child table, the invoke singleton and the per-key/global caps -- and is always killed and reaped
when the probe ends (result, error, timeout, cancellation or session loss).

Transport is the MCP stdio framing: one JSON-RPC message per line. Uses the initialize
handshake (``2025-11-25`` / peer-chosen version). ``server/discover`` (2026-07-28) is not
sent first: several shipping servers ignore or crash on unknown methods (e.g.
``mcp-server-cvm``), which previously burned the whole probe budget as ``upstream_timeout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from portunus_shared import PortunusError

MODERN_PROTOCOL_VERSION = "2026-07-28"
HANDSHAKE_PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION = HANDSHAKE_PROTOCOL_VERSION  # legacy alias
CLIENT_INFO = {"name": "portunus-probe", "version": "1"}
MAX_PAGES = 50
MAX_TOOLS = 2000
MAX_RESULT_BYTES = 4 * 1024 * 1024  # stays under the 5MiB wire frame with envelope room
# Many pre-2026 servers ignore unknown methods instead of returning -32601. Cap the
# discover attempt so those peers fall back to initialize instead of burning the
# whole probe budget (seen with mcp-server-cvm / alibabacloud.mcp-proxy).
DISCOVER_BUDGET_S = 2.0


def _crash(message: str, *, cause: str | None = None) -> PortunusError:
    return PortunusError(
        "upstream_crash",
        message,
        child_exited=cause == "child_exited",
    )


class MethodError(PortunusError):
    """JSON-RPC error from a child; ``jsonrpc_code`` is the numeric code when present."""

    def __init__(self, method: str, jsonrpc_code: int | None, text: Any) -> None:
        super().__init__("upstream_crash", f"{method} failed: {str(text)[:200]}")
        self.jsonrpc_code = jsonrpc_code


class _Stdio:
    OVERSIZE_CODE = "upstream_crash"  # code for a child line over the reader limit

    def __init__(
        self,
        proc: Any,
        *,
        client_info: dict[str, str] | None = None,
        stderr_tail: Callable[[], str] | None = None,
    ) -> None:
        self._proc = proc
        self._stderr_tail = stderr_tail
        self._next_id = 0
        self.modern = False
        self.protocol = MODERN_PROTOCOL_VERSION
        self.client_info = dict(client_info or CLIENT_INFO)

    def _client_meta(self) -> dict[str, Any]:
        return {
            "io.modelcontextprotocol/protocolVersion": self.protocol,
            "io.modelcontextprotocol/clientInfo": dict(self.client_info),
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    async def _write(self, obj: dict) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode() + b"\n"
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (ConnectionError, OSError):
            raise _crash("child closed stdin", cause="child_exited") from None

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def call(self, method: str, params: dict | None = None) -> Any:
        self._next_id += 1
        mid = self._next_id
        wire_params = dict(params or {})
        if self.modern:
            meta = wire_params.get("_meta")
            if not isinstance(meta, dict):
                meta = {}
            wire_params["_meta"] = {**meta, **self._client_meta()}
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}
        if wire_params or params is not None:
            msg["params"] = wire_params
        await self._write(msg)
        while True:
            try:
                line = await self._proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raise PortunusError(self.OVERSIZE_CODE, "child sent an oversize line") from None
            if not line:
                # Prefer ProcessHandle.stderr_tail — never compete with its drain
                # reader on the same StreamReader.
                tail = ""
                if self._stderr_tail is not None:
                    with contextlib.suppress(Exception):
                        tail = (self._stderr_tail() or "").strip()
                detail = (
                    f"child exited before answering: {tail[-400:]}"
                    if tail
                    else "child exited before answering"
                )
                raise _crash(detail, cause="child_exited")
            try:
                reply = json.loads(line)
            except ValueError:
                continue  # stray non-protocol output on stdout
            if not isinstance(reply, dict):
                continue
            if "method" in reply:
                # Server-initiated message. Answer requests so a chatty server cannot stall.
                if "id" in reply:
                    result = {"result": {}} if reply["method"] == "ping" else {
                        "error": {"code": -32601, "message": "method not supported"}
                    }
                    await self._write({"jsonrpc": "2.0", "id": reply["id"], **result})
                continue
            if reply.get("id") != mid:
                continue
            if "error" in reply:
                err = reply["error"]
                text = err.get("message") if isinstance(err, dict) else None
                code = err.get("code") if isinstance(err, dict) else None
                raise MethodError(method, code if isinstance(code, int) else None, text)
            return reply.get("result")


async def _legacy_handshake(rpc: _Stdio) -> None:
    rpc.modern = False
    rpc.protocol = HANDSHAKE_PROTOCOL_VERSION
    init = await rpc.call(
        "initialize",
        {
            "protocolVersion": HANDSHAKE_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": dict(rpc.client_info),
        },
    )
    if not isinstance(init, dict):
        raise _crash("malformed initialize result")
    proto = init.get("protocolVersion")
    if isinstance(proto, str) and proto:
        rpc.protocol = proto
    await rpc.notify("notifications/initialized")


async def connect_stdio(rpc: _Stdio) -> None:
    """Negotiate protocol via initialize handshake.

    ``server/discover`` is intentionally not probed first: peers that neither
    implement it nor return ``-32601`` can hang or crash (see module docstring).
    """
    await _legacy_handshake(rpc)


async def connect_stdio_prefer_discover(rpc: _Stdio) -> None:
    """Like ``connect_stdio`` but try ``server/discover`` first (short budget).

    Kept for modern peers / tests; production probe/rpc use ``connect_stdio``.
    """
    rpc.modern = True
    rpc.protocol = MODERN_PROTOCOL_VERSION
    try:
        async with asyncio.timeout(DISCOVER_BUDGET_S):
            result = await rpc.call("server/discover", {})
    except TimeoutError:
        await _legacy_handshake(rpc)
        return
    except MethodError as e:
        if e.jsonrpc_code not in (-32601, -32600, None):
            raise
        await _legacy_handshake(rpc)
        return
    if not isinstance(result, dict):
        raise _crash("malformed server/discover result")
    versions = result.get("supportedVersions") or result.get("supported_versions")
    if isinstance(versions, list) and versions and MODERN_PROTOCOL_VERSION not in versions:
        await _legacy_handshake(rpc)
        return


async def list_tools(
    proc: Any,
    timeout_s: float,
    *,
    stderr_tail: Callable[[], str] | None = None,
) -> list[dict]:
    """Run connect + tools/list (all pages) against ``proc`` within ``timeout_s``.

    Raises ``upstream_timeout`` / ``upstream_crash`` / ``payload_too_large``.
    """
    try:
        async with asyncio.timeout(timeout_s):
            rpc = _Stdio(proc, stderr_tail=stderr_tail)
            await connect_stdio(rpc)

            tools: list[dict] = []
            cursor: str | None = None
            for _page in range(MAX_PAGES):
                res = await rpc.call("tools/list", {"cursor": cursor} if cursor else {})
                page = res.get("tools") if isinstance(res, dict) else None
                if not isinstance(page, list):
                    raise _crash("malformed tools/list result")
                tools.extend(page)
                if len(tools) > MAX_TOOLS:
                    raise PortunusError("payload_too_large", "too many tools")
                cursor = res.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str):
                    raise _crash("malformed nextCursor")
            else:
                raise _crash("tools/list pagination too long")
            return tools
    except TimeoutError:
        raise PortunusError("upstream_timeout", "probe timed out") from None
