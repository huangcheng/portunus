"""Classic MCP SSE transport (GET /sse + POST /message) — Youdao-style peers."""

from __future__ import annotations

import json

import httpx
import pytest
from portunus_shared import PortunusError

from portunus_gateway.remote import RemoteClient, RemotePolicy
from test_remote_ssrf import PUBLIC, static_resolver

pytestmark = pytest.mark.anyio

TOOLS = [{"name": "listNotes", "inputSchema": {"type": "object", "properties": {}}}]


def _sse_handler(seen: list[httpx.Request]):
    session = {"id": "sess-1", "opened": False}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/sse"):
            session["opened"] = True
            body = (
                "event:endpoint\n"
                "data:/api/ynote/mcp/message?sessionId=sess-1\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )
        if request.method == "POST" and "/message" in path:
            if not session["opened"]:
                return httpx.Response(404, json={"error": "no session"})
            msg = json.loads(request.content)
            method = msg.get("method")
            mid = msg.get("id")
            if mid is None:
                return httpx.Response(202)
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp-server", "version": "1.0.0"},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            else:
                result = {"content": [{"type": "text", "text": "ok"}]}
            # Classic SSE delivers the JSON-RPC result on the still-open GET stream.
            # Our mock cannot push into a closed GET body, so echo on the POST body
            # (RemoteClient accepts either).
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": mid, "result": result},
            )
        return httpx.Response(404, json={"error": "missing"})

    return handler


async def test_classic_sse_list_tools():
    seen: list[httpx.Request] = []
    client = RemoteClient(
        "https://open.mail.163.com/api/ynote/mcp/sse",
        headers={"x-api-key": "k"},
        policy=RemotePolicy(),
        transport=httpx.MockTransport(_sse_handler(seen)),
        resolver=static_resolver({"open.mail.163.com": [PUBLIC]}),
    )
    tools = await client.list_tools()
    assert [t["name"] for t in tools] == ["listNotes"]
    assert any(r.method == "GET" and r.url.path.endswith("/sse") for r in seen)
    assert any(r.method == "POST" and "/message" in r.url.path for r in seen)
    assert all(r.headers.get("x-api-key") == "k" for r in seen)


async def test_classic_sse_auth_required_on_get():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = RemoteClient(
        "https://open.mail.163.com/api/ynote/mcp/sse",
        headers={"x-api-key": "bad"},
        policy=RemotePolicy(),
        transport=httpx.MockTransport(handler),
        resolver=static_resolver({"open.mail.163.com": [PUBLIC]}),
    )
    with pytest.raises(PortunusError) as ei:
        await client.list_tools()
    assert ei.value.code == "auth_required"


def _xunlei_sse_handler(seen: list[httpx.Request]):
    """Xunlei-style: GET …/models/sse/<token>, endpoint event under same prefix."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "GET" and "/sse/" in path:
            body = (
                "event:endpoint\n"
                "data:/models/message?sessionId=xl-1\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )
        if request.method == "POST" and "/message" in path:
            msg = json.loads(request.content)
            method = msg.get("method")
            mid = msg.get("id")
            if mid is None:
                return httpx.Response(202)
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xunlei-mcp", "version": "1.0.0"},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            else:
                result = {"content": [{"type": "text", "text": "ok"}]}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": mid, "result": result},
            )
        return httpx.Response(404, json={"error": "missing"})

    return handler


async def test_classic_sse_credential_in_path():
    """Xunlei pastes …/models/sse/<token>; must not use Streamable HTTP."""
    from portunus_gateway.remote import _is_classic_sse_url

    url = "https://api-xmodels.xunlei.com/models/sse/tok_example_not_real"
    assert _is_classic_sse_url(url) is True
    assert _is_classic_sse_url("https://api.example/mcp") is False

    seen: list[httpx.Request] = []
    client = RemoteClient(
        url,
        headers={},
        policy=RemotePolicy(),
        transport=httpx.MockTransport(_xunlei_sse_handler(seen)),
        resolver=static_resolver({"api-xmodels.xunlei.com": [PUBLIC]}),
    )
    tools = await client.list_tools()
    assert [t["name"] for t in tools] == ["listNotes"]
    assert any(r.method == "GET" and "/sse/" in r.url.path for r in seen)
    assert any(r.method == "POST" and "/message" in r.url.path for r in seen)
