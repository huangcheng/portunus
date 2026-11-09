"""Ephemeral MCP ``tools/list`` probe over a freshly spawned stdio child (spec §5, §6.2).

A probe is dashboard/scheduler-only: the child lives in the ``probe`` namespace -- outside the
child table, the invoke singleton and the per-key/global caps -- and is always killed and reaped
when the probe ends (result, error, timeout, cancellation or session loss).

Transport is the MCP stdio framing: one JSON-RPC message per line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from portunus_shared import PortunusError

PROTOCOL_VERSION = "2025-06-18"
MAX_PAGES = 50
MAX_TOOLS = 2000
MAX_RESULT_BYTES = 4 * 1024 * 1024  # stays under the 5MiB wire frame with envelope room


def _crash(message: str) -> PortunusError:
    return PortunusError("upstream_crash", message)


class _Stdio:
    def __init__(self, proc: Any) -> None:
        self._proc = proc
        self._next_id = 0

    async def _write(self, obj: dict) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode() + b"\n"
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (ConnectionError, OSError):
            raise _crash("child closed stdin") from None

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def call(self, method: str, params: dict | None = None) -> Any:
        self._next_id += 1
        mid = self._next_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)
        while True:
            try:
                line = await self._proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raise _crash("child sent an oversize line") from None
            if not line:
                raise _crash("child exited before answering")
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
                raise PortunusError("upstream_crash", f"{method} failed: {str(text)[:200]}")
            return reply.get("result")


async def list_tools(proc: Any, timeout_s: float) -> list[dict]:
    """Run initialize + tools/list (all pages) against ``proc`` within ``timeout_s``.

    Raises ``upstream_timeout`` / ``upstream_crash`` / ``payload_too_large``.
    """
    try:
        async with asyncio.timeout(timeout_s):
            rpc = _Stdio(proc)
            init = await rpc.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "portunus-probe", "version": "1"},
                },
            )
            if not isinstance(init, dict):
                raise _crash("malformed initialize result")
            await rpc.notify("notifications/initialized")

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
                raise _crash("tools/list pagination did not terminate")
    except TimeoutError:
        raise PortunusError("upstream_timeout", "probe timed out") from None
    if len(json.dumps(tools, separators=(",", ":"))) > MAX_RESULT_BYTES:
        raise PortunusError("payload_too_large", "tools/list result too large")
    return tools
