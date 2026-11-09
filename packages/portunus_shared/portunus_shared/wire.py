"""Gateway<->runner wire codec (spec §6.2): BE u32 length + UTF-8 JSON, max 5MiB body."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from .errors import PortunusError

WIRE_VERSION = 1
MAX_FRAME_BODY = 5 * 1024 * 1024

OPS: frozenset[str] = frozenset(
    {
        "hello",
        "inventory",
        "start",
        "stop",
        "kill",
        "probe",
        "rpc",
        "cancel",
        "heartbeat",
        "child_exited",
    }
)


class FrameError(PortunusError):
    """Malformed or oversize frame. Oversize -> payload_too_large; otherwise unsupported."""

    def __init__(self, message: str = "", code: str = "unsupported") -> None:
        super().__init__(code, message)


class _Reader(Protocol):
    async def readexactly(self, n: int) -> bytes: ...


def encode_frame(obj: dict) -> bytes:
    """Encode obj as 4-byte big-endian length + compact UTF-8 JSON. Raises FrameError if body > 5MiB."""
    if not isinstance(obj, dict):
        raise FrameError("frame must be a JSON object")
    try:
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise FrameError(f"frame not serializable: {e}") from e
    if len(body) > MAX_FRAME_BODY:
        raise FrameError(
            f"frame body {len(body)} exceeds max {MAX_FRAME_BODY}", code="payload_too_large"
        )
    return len(body).to_bytes(4, "big") + body


async def read_frame(reader: asyncio.StreamReader | _Reader) -> dict:
    """Read one frame. EOF raises asyncio.IncompleteReadError; bad frames raise FrameError.

    The length header is validated before the body is read, so an oversize
    declaration never causes a large read.
    """
    header = await reader.readexactly(4)
    n = int.from_bytes(header, "big")
    if n > MAX_FRAME_BODY:
        raise FrameError(f"frame body {n} exceeds max {MAX_FRAME_BODY}", code="payload_too_large")
    body = await reader.readexactly(n)
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise FrameError(f"invalid frame body: {e}") from e
    if not isinstance(obj, dict):
        raise FrameError("frame must be a JSON object")
    return obj


def validate_message(msg: dict) -> None:
    """Check envelope: version == 1 and op in OPS."""
    if msg.get("version") != WIRE_VERSION:
        raise FrameError(f"unsupported wire version: {msg.get('version')!r}")
    if msg.get("op") not in OPS:
        raise FrameError(f"unknown op: {msg.get('op')!r}")


def _msg(
    op: str,
    request_id: str,
    token: str,
    gateway_epoch: str | None = None,
    conn_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    # Token rides on every frame; gateway_epoch/conn_id on gateway->runner requests.
    m: dict[str, Any] = {
        "version": WIRE_VERSION,
        "op": op,
        "request_id": request_id,
        "token": token,
    }
    if gateway_epoch is not None:
        m["gateway_epoch"] = gateway_epoch
    if conn_id is not None:
        m["conn_id"] = conn_id
    m.update(fields)
    return m


def hello(*, request_id: str, token: str, **extra: Any) -> dict[str, Any]:
    return _msg("hello", request_id, token, **extra)


def inventory(*, request_id: str, token: str, **extra: Any) -> dict[str, Any]:
    return _msg("inventory", request_id, token, **extra)


def start(
    *,
    request_id: str,
    token: str,
    child_id: str,
    server_id: str,
    argv: list[str],
    trailing_allowlist: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _msg(
        "start",
        request_id,
        token,
        child_id=child_id,
        server_id=server_id,
        argv=list(argv),
        trailing_allowlist=list(trailing_allowlist or []),
        **extra,
    )


def stop(*, request_id: str, token: str, child_id: str, **extra: Any) -> dict[str, Any]:
    return _msg("stop", request_id, token, child_id=child_id, **extra)


def kill(*, request_id: str, token: str, child_id: str, **extra: Any) -> dict[str, Any]:
    return _msg("kill", request_id, token, child_id=child_id, **extra)


def probe(*, request_id: str, token: str, child_id: str, **extra: Any) -> dict[str, Any]:
    return _msg("probe", request_id, token, child_id=child_id, **extra)


def rpc(
    *, request_id: str, token: str, child_id: str, payload: dict, **extra: Any
) -> dict[str, Any]:
    return _msg("rpc", request_id, token, child_id=child_id, payload=payload, **extra)


def cancel(
    *, request_id: str, token: str, target_request_id: str, **extra: Any
) -> dict[str, Any]:
    return _msg("cancel", request_id, token, target_request_id=target_request_id, **extra)


def heartbeat(*, request_id: str, token: str, **extra: Any) -> dict[str, Any]:
    return _msg("heartbeat", request_id, token, **extra)


def child_exited(
    *, request_id: str, token: str, child_id: str, exit_code: int | None = None, **extra: Any
) -> dict[str, Any]:
    return _msg("child_exited", request_id, token, child_id=child_id, exit_code=exit_code, **extra)
