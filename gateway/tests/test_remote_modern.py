"""Modern (MCP 2026-07-28) client-side duties of ``RemoteClient`` (audit 2026-11, M1-M3).

* ``x-mcp-header`` tool parameters are mirrored into ``Mcp-Param-*`` headers, with the
  ``=?base64?...?=`` sentinel for values that are not plain-ASCII-safe.
* Tools whose ``inputSchema`` carries an invalid ``x-mcp-header`` annotation are excluded
  from ``tools/list`` (the definition is invalid as a whole on Streamable HTTP).
* ``Mcp-Name`` uses the same sentinel encoding.
* Discover 4xx bodies are inspected before any legacy fallback: ``-32022`` follows the
  advertised version list, ``-32020`` / ``-32021`` surface as errors (modern server).
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from mcp.shared.inbound import encode_header_value
from portunus_shared import PortunusError
from test_remote_ssrf import PUBLIC, static_resolver

from portunus_gateway.remote import RemoteClient

pytestmark = pytest.mark.anyio

MODERN = "2026-07-28"

HEADER_SCHEMA = {
    "type": "object",
    "properties": {
        "q": {"type": "string", "x-mcp-header": "X-Query"},
        "n": {"type": "integer", "x-mcp-header": "X-Count"},
        "flag": {"type": "boolean", "x-mcp-header": "X-Flag"},
    },
}


def modern_peer(
    seen: list[httpx.Request],
    *,
    discover: httpx.Response | None = None,
    tools: list[dict] | None = None,
    initialize_protocol: str | None = None,
):
    """A modern-era MCP peer on MockTransport; records every request into ``seen``."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            return httpx.Response(202)
        if method == "server/discover":
            if discover is not None:
                return discover
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "supportedVersions": [MODERN],
                        "capabilities": {"tools": {}},
                        "resultType": "complete",
                    },
                },
            )
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-legacy"},
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"protocolVersion": initialize_protocol or "2025-11-25"},
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"tools": tools if tools is not None else [], "resultType": "complete"},
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": "ok"}], "resultType": "complete"},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601}})

    return handler


def client(handler) -> RemoteClient:
    return RemoteClient(
        "https://mcp.example.com/mcp",
        headers={},
        transport=httpx.MockTransport(handler),
        resolver=static_resolver({"mcp.example.com": [PUBLIC]}),
    )


def methods_of(seen: list[httpx.Request]) -> list[str]:
    return [json.loads(r.content).get("method") for r in seen if r.content]


# ---- M1 + M3: Mcp-Param mirroring and value encoding --------------------------------------


async def test_call_mirrors_x_mcp_header_params():
    seen: list[httpx.Request] = []
    c = client(modern_peer(seen))
    out = await c.call_tool(
        "alpha",
        {"q": "héllo", "n": 42, "flag": True},
        input_schema=HEADER_SCHEMA,
    )
    assert out["content"][0]["text"] == "ok"
    call = seen[-1]
    assert call.headers["mcp-param-x-query"] == encode_header_value("héllo")
    assert call.headers["mcp-param-x-query"].startswith("=?base64?")
    assert call.headers["mcp-param-x-count"] == "42"
    assert call.headers["mcp-param-x-flag"] == "true"
    # Plain-ASCII values pass verbatim (no sentinel).
    seen.clear()
    await c.call_tool("alpha", {"q": "simple"}, input_schema=HEADER_SCHEMA)
    assert seen[-1].headers["mcp-param-x-query"] == "simple"
    assert seen[-1].headers.get("mcp-param-x-count") is None


async def test_call_omits_param_headers_for_absent_and_null_args():
    seen: list[httpx.Request] = []
    c = client(modern_peer(seen))
    await c.call_tool("alpha", {"n": None}, input_schema=HEADER_SCHEMA)
    headers = seen[-1].headers
    assert "mcp-param-x-query" not in headers
    assert "mcp-param-x-count" not in headers  # null → omitted, not the string "None"
    assert "mcp-param-x-flag" not in headers


async def test_call_does_not_mirror_from_invalid_schema():
    """A schema whose annotations are invalid yields no headers (mirroring subset undefined)."""
    seen: list[httpx.Request] = []
    c = client(modern_peer(seen))
    bad = {
        "type": "object",
        "properties": {"q": {"type": "string", "x-mcp-header": "not a token!"}},
    }
    await c.call_tool("alpha", {"q": "x"}, input_schema=bad)
    assert "mcp-param-not a token!" not in seen[-1].headers


# ---- M1: tools/list filtering ------------------------------------------------------------


async def test_list_tools_drops_invalid_x_mcp_header_tools(caplog):
    tools = [
        {"name": "good", "inputSchema": {"type": "object"}},
        # Empty token is not an RFC 9110 field name.
        {"name": "empty", "inputSchema": {"type": "object", "properties": {"q": {"type": "string", "x-mcp-header": ""}}}},
        # Annotation under `items` is not statically reachable via pure `properties`.
        {
            "name": "nested",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "xs": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"q": {"type": "string", "x-mcp-header": "X-Q"}}},
                    }
                },
            },
        },
    ]
    seen: list[httpx.Request] = []
    with caplog.at_level(logging.WARNING, logger="portunus.remote"):
        got = await client(modern_peer(seen, tools=tools)).list_tools()
    assert [t["name"] for t in got] == ["good"]
    dropped = [r.getMessage() for r in caplog.records if "dropping tool" in r.getMessage()]
    assert len(dropped) == 2, dropped


# ---- M3: Mcp-Name sentinel -----------------------------------------------------------------


async def test_non_ascii_tool_name_uses_base64_sentinel():
    seen: list[httpx.Request] = []
    await client(modern_peer(seen)).call_tool("outils_héllo", {})
    assert seen[-1].headers["mcp-name"] == encode_header_value("outils_héllo")
    assert seen[-1].headers["mcp-name"].startswith("=?base64?")


# ---- M2: era fallback honors modern errors -------------------------------------------------


async def test_discover_32022_with_advertised_handshake_uses_it():
    """-32022 listing a handshake revision: initialize offers *that* revision, not the newest."""
    seen: list[httpx.Request] = []
    discover = httpx.Response(
        400,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supported": ["2024-11-05", "2025-06-18"], "requested": MODERN},
            },
        },
    )
    out = await client(modern_peer(seen, discover=discover, initialize_protocol="2025-06-18")).call_tool(
        "alpha", {}
    )
    assert out["content"][0]["text"] == "ok"
    init = next(r for r in seen if json.loads(r.content).get("method") == "initialize")
    assert json.loads(init.content)["params"]["protocolVersion"] == "2025-06-18"
    assert init.headers["mcp-protocol-version"] == "2025-06-18"
    assert methods_of(seen)[0] == "server/discover"


async def test_discover_32022_without_overlap_raises():
    seen: list[httpx.Request] = []
    discover = httpx.Response(
        400,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supported": ["2030-01-01"], "requested": MODERN},
            },
        },
    )
    with pytest.raises(PortunusError) as ei:
        await client(modern_peer(seen, discover=discover)).call_tool("alpha", {})
    assert ei.value.code == "unsupported"
    assert "2030-01-01" in ei.value.message
    assert methods_of(seen) == ["server/discover"]  # no initialize handshake attempted


async def test_discover_400_header_mismatch_does_not_fall_back():
    """-32020 in the 400 body marks a modern server; falling back would erase the cause."""
    seen: list[httpx.Request] = []
    discover = httpx.Response(
        400,
        json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32020, "message": "HeaderMismatch"}},
    )
    with pytest.raises(PortunusError) as ei:
        await client(modern_peer(seen, discover=discover)).call_tool("alpha", {})
    assert ei.value.code == "unsupported"
    assert methods_of(seen) == ["server/discover"]


async def test_discover_200_error_32021_surfaces_without_fallback():
    seen: list[httpx.Request] = []
    discover = httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32021, "message": "MissingRequiredClientCapability"},
        },
    )
    with pytest.raises(PortunusError) as ei:
        await client(modern_peer(seen, discover=discover)).call_tool("alpha", {})
    assert ei.value.code == "unsupported"
    assert methods_of(seen) == ["server/discover"]


async def test_plain_400_text_body_still_falls_back():
    """Non-JSON 4xx bodies keep the pre-2026 fallback (Flomo-style peers)."""
    seen: list[httpx.Request] = []
    discover = httpx.Response(400, content=b"Bad Request")
    out = await client(modern_peer(seen, discover=discover)).call_tool("alpha", {})
    assert out["content"][0]["text"] == "ok"
    assert "initialize" in methods_of(seen)


# ---- redirects: mirrored headers survive cross-origin hops ---------------------------------


async def test_mcp_param_headers_survive_cross_origin_redirect():
    """The body (with arguments) follows the redirect; the mirrored headers must follow it too."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Requests are pinned to the resolved IP; branch on the Host header (the origin name).
        if request.headers.get("host") == "mcp.example.com":
            return httpx.Response(307, headers={"Location": "https://other.example.com/mcp"})
        msg = json.loads(request.content)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            return httpx.Response(202)
        if method == "server/discover":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "supportedVersions": [MODERN],
                        "capabilities": {"tools": {}},
                        "resultType": "complete",
                    },
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": "ok"}], "resultType": "complete"},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601}})

    c = RemoteClient(
        "https://mcp.example.com/mcp",
        headers={"x-vault-secret": "cred"},
        transport=httpx.MockTransport(handler),
        resolver=static_resolver({"mcp.example.com": [PUBLIC], "other.example.com": [PUBLIC]}),
    )
    out = await c.call_tool("alpha", {"q": "v"}, input_schema=HEADER_SCHEMA)
    assert out["content"][0]["text"] == "ok"
    # Requests are pinned to the resolved IP; the Host header names the hop's origin.
    redirected = next(
        r
        for r in seen
        if r.headers.get("host") == "other.example.com"
        and r.content
        and json.loads(r.content).get("method") == "tools/call"
    )
    assert redirected.headers["mcp-param-x-query"] == "v"  # mirror follows the body
    assert redirected.headers.get("x-vault-secret") is None  # credentials never do


def test_format_rpc_error_unwraps_qqmail_nested_auth():
    from portunus_gateway.remote import _format_rpc_error, _portunus_error_from_rpc

    err = {
        "code": -32000,
        "message": json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "error": {
                                    "code": 40101,
                                    "message": "Invalid or expired access token",
                                }
                            }
                        ),
                    }
                ]
            }
        ),
    }
    text = _format_rpc_error(err)
    assert "Invalid or expired access token" in text
    assert "40101" in text
    mapped = _portunus_error_from_rpc(err)
    assert mapped.code == "auth_required"
