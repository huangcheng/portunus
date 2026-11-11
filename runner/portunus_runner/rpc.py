"""``rpc`` op: bridge a single MCP ``tools/call`` to a tracked invoke child (spec §6.2).

Only ``tools/call`` is bridged -- the gateway never needs the child to run anything else, and a
compromised gateway-side bug must not be able to drive arbitrary JSON-RPC at a child. The child is
initialised lazily on its first call and calls are serialised per child (one child per
``(server, key)`` pair).

A child line longer than the stdout reader limit surfaces as ``payload_too_large``; the stream is
then desynchronised mid-line, so the caller must kill the child (see ``Runner._do_rpc``).

Timing (attribution): ``queue_ms`` waits for the per-child lock, ``init_ms`` is the lazy MCP
handshake on first call, ``upstream_ms`` is the ``tools/call`` itself. Returned beside the result
so the gateway can attach them to spans without an OTel SDK in the runner.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from portunus_shared import PortunusError

from .probe import _crash, _Stdio, connect_stdio

MAX_TIMEOUT_S = 600.0


class _RpcStdio(_Stdio):
    OVERSIZE_CODE = "payload_too_large"


def _ms_since(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))


class ChildRpc:
    def __init__(self, proc: Any) -> None:
        self._stdio = _RpcStdio(proc, client_info={"name": "portunus-invoke", "version": "1"})
        self._lock = asyncio.Lock()
        self._ready = False
        self.last_timing: dict[str, int] = {}

    async def call(self, payload: Any, timeout_s: float) -> tuple[dict, dict[str, int]]:
        """Run the ``tools/call`` described by ``payload``.

        Returns ``(result, timing)`` where timing always includes ``queue_ms`` and
        ``upstream_ms``, and ``init_ms`` on the first call that runs the handshake.
        ``last_timing`` is updated as phases complete so error replies can still report it.
        """
        method = payload.get("method") if isinstance(payload, dict) else None
        params = payload.get("params") if isinstance(payload, dict) else None
        if (
            method != "tools/call"
            or not isinstance(params, dict)
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments", {}), dict)
        ):
            raise PortunusError("unsupported", "rpc payload must be a tools/call request")
        timing: dict[str, int] = {}
        self.last_timing = timing
        t_wait = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                async with self._lock:
                    timing["queue_ms"] = _ms_since(t_wait)
                    if not self._ready:
                        t_init = time.perf_counter()
                        await connect_stdio(self._stdio)
                        timing["init_ms"] = _ms_since(t_init)
                        self._ready = True
                    t_up = time.perf_counter()
                    result = await self._stdio.call("tools/call", params)
                    timing["upstream_ms"] = _ms_since(t_up)
        except TimeoutError:
            raise PortunusError("upstream_timeout", "tools/call timed out") from None
        if not isinstance(result, dict):
            raise _crash("malformed tools/call result")
        return result, timing


def clamp_timeout(raw: Any, default: float) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return min(float(raw), MAX_TIMEOUT_S)
    return default
