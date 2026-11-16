"""Gate-level MCP 2026-07-28 wire rules that must hold before the SDK transport runs.

Covers the two MUSTs the transport itself cannot enforce (audit 2026-11):

* HTTP guard error codes live outside the JSON-RPC / MCP reserved ranges —
  ``-32000..-32019`` is legacy (no new allocations; ``-32002`` must never be emitted)
  and ``-32020..-32099`` is spec-reserved.
* ``MCP-Protocol-Version`` must equal ``params._meta``'s
  ``io.modelcontextprotocol/protocolVersion``; the SDK era-routes on the header alone,
  so a legacy-version header over a modern envelope would be served under legacy rules.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.types import Message

import portunus_gateway.omni as omni_module
from portunus_gateway.omni import _GUARD_CODES_RESERVED_RANGES, _GUARD_RPC_CODES, _Gate

MODERN = "2026-07-28"
META_KEY = "io.modelcontextprotocol/protocolVersion"


@pytest.fixture(autouse=True)
def _valid_key(monkeypatch):
    """Bypass the key store: these tests exercise wire rules, not token verification."""

    async def fake_verify(token: str):
        return SimpleNamespace(key_id="k")

    monkeypatch.setattr(omni_module, "verify_agent_token", fake_verify)


@pytest.fixture
def gate():
    """A gate whose manager records hand-offs; reaching it means validation passed."""

    class _Mgr:
        def __init__(self) -> None:
            self.reached = 0

        async def handle_request(self, scope, receive, send) -> None:
            self.reached += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    mgr = _Mgr()
    return _Gate(mgr)  # type: ignore[arg-type]


async def drive(gate, body: bytes, headers: dict[str, str]):
    """POST ``body`` through the gate with a valid bearer; returns (messages, manager reached)."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg: Message) -> None:
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"authorization", b"Bearer pm-test"),
            *[(k.lower().encode(), v.encode()) for k, v in headers.items()],
        ],
    }
    await gate(scope, receive, send)
    return sent


def status_of(sent: list[Message]) -> int:
    return next(m for m in sent if m["type"] == "http.response.start")["status"]


def body_of(sent: list[Message]) -> dict:
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return json.loads(raw)


def request(method: str, version: str | None = MODERN, rid=1) -> bytes:
    params: dict = {}
    if version is not None:
        params["_meta"] = {
            META_KEY: version,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()


BASE = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# ---- guard error-code partition ----------------------------------------------------------


def test_guard_codes_outside_reserved_ranges():
    for code in [*_GUARD_RPC_CODES.values()]:
        for low, high in _GUARD_CODES_RESERVED_RANGES:
            assert not (low <= code <= high), (code, low, high)


def test_content_length_parse_does_not_raise():
    assert omni_module._declared_body_too_large("²") is False
    assert omni_module._declared_body_too_large("9" * 5000) is True
    assert omni_module._declared_body_too_large("000000000000000000001") is False
    assert omni_module._declared_body_too_large(str(omni_module.MAX_BODY_BYTES + 1)) is True
    assert omni_module._declared_body_too_large(str(omni_module.MAX_BODY_BYTES)) is False


def test_no_guard_code_is_minus_32002():
    """The spec names -32002 explicitly: implementations of this version MUST NOT emit it."""
    assert -32002 not in _GUARD_RPC_CODES.values()


# ---- header / _meta protocol-version cross-check -----------------------------------------


@pytest.mark.anyio
async def test_legacy_header_over_modern_envelope_is_header_mismatch(gate):
    """Header names a handshake version while ``_meta`` names a modern one: must be 400 -32020.

    Regression for audit probe case C: the SDK era-routes on the header alone and the
    legacy path never compares, so this used to be served as HTTP 200.
    """
    sent = await drive(gate, request("tools/list"), {**BASE, "MCP-Protocol-Version": "2025-11-25"})
    assert status_of(sent) == 400
    err = body_of(sent)["error"]
    assert err["code"] == -32020
    assert "protocol version" in err["message"]
    assert body_of(sent)["id"] == 1
    assert gate._manager.reached == 0


@pytest.mark.anyio
async def test_missing_header_over_modern_envelope_is_header_mismatch(gate):
    sent = await drive(gate, request("tools/list"), BASE)
    assert status_of(sent) == 400
    assert body_of(sent)["error"]["code"] == -32020
    assert gate._manager.reached == 0


@pytest.mark.anyio
async def test_matching_header_reaches_the_transport(gate):
    sent = await drive(gate, request("tools/list"), {**BASE, "MCP-Protocol-Version": MODERN})
    assert status_of(sent) == 200
    assert gate._manager.reached == 1


@pytest.mark.anyio
async def test_legacy_initialize_without_meta_version_passes(gate):
    """Initialize-era traffic carries ``protocolVersion`` top-level, not in ``_meta``."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
    ).encode()
    sent = await drive(gate, body, {**BASE, "MCP-Protocol-Version": "2025-06-18"})
    assert status_of(sent) == 200
    assert gate._manager.reached == 1


@pytest.mark.anyio
async def test_notification_envelope_with_missing_header_is_rejected(gate):
    """Modern notifications MUST carry the header (SH-27); refuse rather than 202 them."""
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"_meta": {META_KEY: MODERN}}}
    ).encode()
    sent = await drive(gate, body, BASE)
    assert status_of(sent) == 400
    assert body_of(sent)["error"]["code"] == -32020
    assert gate._manager.reached == 0


@pytest.mark.anyio
async def test_null_body_is_invalid_envelope_not_forwarded(gate):
    """JSON ``null`` decodes to ``None``; it must not be confused with a parse failure."""
    sent = await drive(gate, b"null", BASE)
    assert status_of(sent) == 400
    assert body_of(sent)["error"]["code"] == -32602
    assert gate._manager.reached == 0


@pytest.mark.anyio
async def test_bom_prefixed_batch_refused_as_batch(gate):
    """A UTF-8 BOM must not turn a batch into a generic envelope error (-32600, not -32602)."""
    body = b"\xef\xbb\xbf" + json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode()
    sent = await drive(gate, body, BASE)
    assert status_of(sent) == 400
    err = body_of(sent)["error"]
    assert err["code"] == -32600
    assert "batch" in err["message"].lower()
    assert gate._manager.reached == 0


@pytest.mark.anyio
async def test_mismatch_reply_correlates_integer_valued_float_id(gate):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2.0,
            "method": "tools/list",
            "params": {"_meta": {META_KEY: MODERN}},
        }
    ).encode()
    sent = await drive(gate, body, BASE)
    assert status_of(sent) == 400
    assert body_of(sent)["id"] == 2
