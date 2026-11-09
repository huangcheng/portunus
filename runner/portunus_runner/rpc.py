"""``rpc`` op: bridge a single MCP ``tools/call`` to a tracked invoke child (spec §6.2).

Only ``tools/call`` is bridged -- the gateway never needs the child to run anything else, and a
compromised gateway-side bug must not be able to drive arbitrary JSON-RPC at a child. The child is
initialised lazily on its first call and calls are serialised per child (one child per
``(server, key)`` pair).

A child line longer than the stdout reader limit surfaces as ``payload_too_large``; the stream is
then desynchronised mid-line, so the caller must kill the child (see ``Runner._do_rpc``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from portunus_shared import PortunusError

from .probe import PROTOCOL_VERSION, _crash, _Stdio

MAX_TIMEOUT_S = 600.0


class _RpcStdio(_Stdio):
    OVERSIZE_CODE = "payload_too_large"


class ChildRpc:
    def __init__(self, proc: Any) -> None:
        self._stdio = _RpcStdio(proc)
        self._lock = asyncio.Lock()
        self._ready = False

    async def _initialize(self) -> None:
        init = await self._stdio.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "portunus-invoke", "version": "1"},
            },
        )
        if not isinstance(init, dict):
            raise _crash("malformed initialize result")
        await self._stdio.notify("notifications/initialized")
        self._ready = True

    async def call(self, payload: Any, timeout_s: float) -> dict:
        """Run the ``tools/call`` described by ``payload``; returns the child's result object."""
        method = payload.get("method") if isinstance(payload, dict) else None
        params = payload.get("params") if isinstance(payload, dict) else None
        if (
            method != "tools/call"
            or not isinstance(params, dict)
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            raise PortunusError("unsupported", "rpc payload must be a tools/call request")
        try:
            async with asyncio.timeout(timeout_s):
                async with self._lock:
                    if not self._ready:
                        await self._initialize()
                    result = await self._stdio.call("tools/call", params)
        except TimeoutError:
            raise PortunusError("upstream_timeout", "tools/call timed out") from None
        if not isinstance(result, dict):
            raise _crash("malformed tools/call result")
        return result


def clamp_timeout(raw: Any, default: float) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return min(float(raw), MAX_TIMEOUT_S)
    return default

