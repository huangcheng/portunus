"""Remote MCP proxy + SSRF controls (Task 11).

Two harnesses: ``httpx.MockTransport`` (observes exactly what would go on the wire: pinned IP,
Host header, SNI, headers per redirect hop) and a real loopback MCP server for the allowlisted
http path and the DB/vault-backed ``RemoteProxy``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid

import httpx
import pytest
from portunus_shared import PortunusError

from portunus_gateway.db.models import Server, VaultEntry
from portunus_gateway.db.session import async_session
from portunus_gateway.registry import RegistryError
from portunus_gateway.remote import (
    RemoteClient,
    RemotePolicy,
    RemoteProxy,
    SsrfError,
    check_address,
)
from portunus_gateway.vault import Vault

pytestmark = pytest.mark.anyio

TOOLS = [{"name": "alpha", "description": "first", "inputSchema": {"type": "object"}}]
PUBLIC = "93.184.216.34"


def static_resolver(mapping: dict[str, list[str]]):
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        return mapping[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def mcp_handler(seen: list[httpx.Request] | None = None, *, redirect=None):
    """MockTransport handler speaking minimal Streamable HTTP MCP (JSON responses)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if redirect is not None:
            r = redirect(request)
            if r is not None:
                return r
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return httpx.Response(202)
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "t", "version": "1"},
                    },
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": json.dumps(msg["params"])}]},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601}})

    return handler


def client(
    url: str,
    *,
    handler=None,
    resolver=None,
    headers: dict[str, str] | None = None,
    policy: RemotePolicy | None = None,
) -> RemoteClient:
    return RemoteClient(
        url,
        headers=headers or {},
        policy=policy or RemotePolicy(),
        transport=httpx.MockTransport(handler or mcp_handler()),
        resolver=resolver or static_resolver({"mcp.example.com": [PUBLIC]}),
    )


# ---- address classification --------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "169.254.169.254",
        "169.254.0.1",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "127.0.0.1",  # loopback, not allowlisted
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "::ffff:169.254.169.254",  # v4-mapped
        "::ffff:127.0.0.1",
        "64:ff9b::a9fe:a9fe",  # NAT64 embedding 169.254.169.254
        "2002:a9fe:a9fe::1",  # 6to4 embedding 169.254.169.254
        "::a9fe:a9fe",  # v4-compatible
    ],
)
def test_non_public_addresses_denied_by_default(addr):
    with pytest.raises(SsrfError):
        check_address(addr, allow_loopback=False)


@pytest.mark.parametrize("addr", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_allowed(addr):
    check_address(addr, allow_loopback=False)


def test_loopback_exception_only_covers_loopback():
    check_address("127.0.0.1", allow_loopback=True)
    check_address("::1", allow_loopback=True)
    for addr in ("169.254.169.254", "10.0.0.1", "192.168.0.1", "fe80::1"):
        with pytest.raises(SsrfError):
            check_address(addr, allow_loopback=True)


def test_ssrf_error_is_a_portunus_error():
    with pytest.raises(PortunusError) as exc:
        check_address("169.254.169.254", allow_loopback=False)
    assert exc.value.code == "unsupported"


# ---- brief: metadata address rejected ----------------------------------------------------


async def test_metadata_ip_literal_rejected_without_any_request():
    seen: list[httpx.Request] = []
    c = client("https://169.254.169.254/latest/meta-data", handler=mcp_handler(seen))
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert seen == []


async def test_metadata_ip_literal_rejected_even_when_loopback_allowlisted():
    seen: list[httpx.Request] = []
    c = client(
        "http://169.254.169.254/",
        handler=mcp_handler(seen),
        policy=RemotePolicy(loopback_allowlist=frozenset({"127.0.0.1", "169.254.169.254"})),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert seen == []


async def test_hostname_resolving_to_metadata_rejected():
    seen: list[httpx.Request] = []
    c = client(
        "https://evil.example.com/mcp",
        handler=mcp_handler(seen),
        resolver=static_resolver({"evil.example.com": ["169.254.169.254"]}),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert seen == []


async def test_mixed_dns_answer_rejected_if_any_address_is_private():
    seen: list[httpx.Request] = []
    c = client(
        "https://mixed.example.com/mcp",
        handler=mcp_handler(seen),
        resolver=static_resolver({"mixed.example.com": [PUBLIC, "10.0.0.5"]}),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert seen == []


async def test_default_resolver_denies_decimal_encoded_loopback():
    # Platforms differ on whether getaddrinfo accepts dotted-decimal integers as
    # hostnames; pin the resolution so the SSRF check is what we assert.
    c = RemoteClient(
        "https://2130706433/mcp",
        headers={},
        policy=RemotePolicy(),
        resolver=static_resolver({"2130706433": ["127.0.0.1"]}),
        transport=httpx.MockTransport(mcp_handler()),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()


async def test_loopback_denied_without_allowlist():
    seen: list[httpx.Request] = []
    for url in ("http://127.0.0.1:9/mcp", "https://127.0.0.1/mcp", "http://[::1]:9/mcp"):
        c = client(url, handler=mcp_handler(seen))
        with pytest.raises(SsrfError):
            await c.list_tools()
    assert seen == []


async def test_allowlist_entry_with_port_does_not_cover_other_ports():
    c = client(
        "http://127.0.0.1:9000/mcp",
        policy=RemotePolicy(loopback_allowlist=frozenset({"127.0.0.1:8000"})),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()


async def test_allowlisted_name_resolving_to_private_non_loopback_still_denied():
    c = client(
        "http://svc.internal/mcp",
        resolver=static_resolver({"svc.internal": ["10.1.2.3"]}),
        policy=RemotePolicy(loopback_allowlist=frozenset({"svc.internal"})),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()


# ---- scheme rules ------------------------------------------------------------------------


async def test_plain_http_rejected_off_loopback_even_for_public_hosts():
    seen: list[httpx.Request] = []
    c = client("http://mcp.example.com/mcp", handler=mcp_handler(seen))
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert seen == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mcp.example.com/mcp",
        "file:///etc/passwd",
        "https://user:pw@mcp.example.com/mcp",
        "https:///mcp",
        "mcp.example.com/mcp",
    ],
)
async def test_bad_urls_rejected(url):
    c = client(url)
    with pytest.raises(SsrfError):
        await c.list_tools()


# ---- success on public https, DNS pinning ------------------------------------------------


async def test_public_https_probe_pins_dns_result_and_keeps_hostname_for_tls():
    seen: list[httpx.Request] = []
    resolver = static_resolver({"mcp.example.com": [PUBLIC]})
    c = client("https://mcp.example.com/mcp", handler=mcp_handler(seen), resolver=resolver)
    tools = await c.list_tools()
    assert tools == TOOLS
    assert seen
    for req in seen:
        # connection goes to the validated IP; Host + SNI keep the name (cert verification)
        assert req.url.host == PUBLIC
        assert req.headers["host"] == "mcp.example.com"
        assert req.extensions["sni_hostname"] == "mcp.example.com"
    # one resolution per hop (each request is its own hop), never reused across redirects
    assert set(resolver.calls) == {"mcp.example.com"}


async def test_ipv6_public_address_is_pinned_with_brackets():
    seen: list[httpx.Request] = []
    c = client(
        "https://v6.example.com/mcp",
        handler=mcp_handler(seen),
        resolver=static_resolver({"v6.example.com": ["2606:4700:4700::1111"]}),
    )
    assert await c.list_tools() == TOOLS
    assert seen[0].url.host == "2606:4700:4700::1111"
    assert seen[0].headers["host"] == "v6.example.com"


async def test_session_flow_initialize_initialized_call_delete():
    seen: list[httpx.Request] = []
    c = client("https://mcp.example.com/mcp", handler=mcp_handler(seen))
    out = await c.call_tool("alpha", {"q": "hi"})
    assert json.loads(out["content"][0]["text"]) == {"name": "alpha", "arguments": {"q": "hi"}}
    methods = [json.loads(r.content).get("method") if r.content else r.method for r in seen]
    # Auto-negotiate probes server/discover, then falls back to the handshake session.
    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/call",
        "DELETE",
    ]
    assert seen[3].headers["mcp-session-id"] == "sess-1"


async def test_modern_2026_discover_skips_handshake():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            return httpx.Response(202)
        if method == "server/discover":
            assert request.headers["mcp-protocol-version"] == "2026-07-28"
            assert request.headers["mcp-method"] == "server/discover"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "supportedVersions": ["2026-07-28"],
                        "capabilities": {"tools": {}},
                        "resultType": "complete",
                    },
                },
            )
        if method == "tools/call":
            assert request.headers["mcp-protocol-version"] == "2026-07-28"
            assert request.headers["mcp-method"] == "tools/call"
            assert request.headers["mcp-name"] == "alpha"
            assert "mcp-session-id" not in request.headers
            meta = (msg.get("params") or {}).get("_meta") or {}
            assert meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "content": [{"type": "text", "text": "ok"}],
                        "resultType": "complete",
                    },
                },
            )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601}}
        )

    out = await client("https://mcp.example.com/mcp", handler=handler).call_tool("alpha", {})
    assert out["content"][0]["text"] == "ok"
    methods = [json.loads(r.content).get("method") for r in seen if r.content]
    assert methods == ["server/discover", "tools/call"]


async def test_discover_http_400_unsupported_protocol_falls_back_to_initialize():
    """Flomo-style peer: rejects 2026-07-28 with plain HTTP 400, speaks initialize."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            return httpx.Response(202)
        if method == "server/discover":
            return httpx.Response(
                400,
                content=b"Bad Request: Unsupported protocol version (supported versions: 2025-11-25)",
            )
        if method == "initialize":
            assert request.headers["mcp-protocol-version"] == "2025-11-25"
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-legacy"},
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
                },
            )
        if method == "tools/call":
            assert request.headers["mcp-session-id"] == "sess-legacy"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": "legacy-ok"}]},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601}})

    out = await client("https://mcp.example.com/mcp", handler=handler).call_tool("alpha", {})
    assert out["content"][0]["text"] == "legacy-ok"
    methods = [json.loads(r.content).get("method") for r in seen if r.content]
    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


async def test_list_tools_follows_pagination():
    pages = {None: ({"tools": [TOOLS[0]], "nextCursor": "c2"}), "c2": {"tools": [{"name": "b"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        if msg.get("id") is None:
            return httpx.Response(202)
        if msg["method"] == "server/discover":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601}}
            )
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        cur = (msg.get("params") or {}).get("cursor")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": msg["id"], "result": pages[cur]}
        )

    tools = await client("https://mcp.example.com/mcp", handler=handler).list_tools()
    assert [t["name"] for t in tools] == ["alpha", "b"]


async def test_sse_response_is_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        if msg.get("id") is None:
            return httpx.Response(202)
        if msg["method"] == "server/discover":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601}},
            )
        result = {} if msg["method"] == "initialize" else {"tools": TOOLS}
        note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
        body = (
            f"event: message\ndata: {json.dumps(note)}\n\n"
            f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    assert await client("https://mcp.example.com/mcp", handler=handler).list_tools() == TOOLS


# ---- redirects ---------------------------------------------------------------------------


def _redirect_once(target: str, status: int = 307):
    state = {"done": False}

    def redirect(request: httpx.Request):
        if request.url.path == "/mcp" and not state["done"]:
            state["done"] = True
            return httpx.Response(status, headers={"location": target})
        return None

    return redirect


SECRET = {"Authorization": "Bearer s3cret", "X-Api-Key": "k-123", "Cookie": "a=b"}


async def test_cross_origin_redirect_strips_credentials():
    seen: list[httpx.Request] = []
    resolver = static_resolver(
        {"mcp.example.com": [PUBLIC], "other.example.org": ["93.184.216.35"]}
    )
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=_redirect_once("https://other.example.org/moved")),
        resolver=resolver,
        headers=SECRET,
    )
    # the first hop is cross-origin redirected; the mock serves the rest at the new location
    await c.list_tools()
    first, second = seen[0], seen[1]
    assert first.headers["authorization"] == "Bearer s3cret"
    assert first.headers["x-api-key"] == "k-123"
    assert second.url.host == "93.184.216.35"
    assert second.headers["host"] == "other.example.org"
    for h in ("authorization", "x-api-key", "cookie", "mcp-session-id"):
        assert h not in second.headers
    assert second.content == first.content  # 307 preserves the body
    assert "mcp.example.com" in resolver.calls and "other.example.org" in resolver.calls


async def test_cross_port_redirect_is_cross_origin():
    seen: list[httpx.Request] = []
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=_redirect_once("https://mcp.example.com:8443/x")),
        headers=SECRET,
    )
    await c.list_tools()
    assert "authorization" not in seen[1].headers


async def test_same_origin_redirect_keeps_credentials():
    seen: list[httpx.Request] = []
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=_redirect_once("https://mcp.example.com/v2/mcp")),
        headers=SECRET,
    )
    await c.list_tools()
    assert seen[1].url.path == "/v2/mcp"
    assert seen[1].headers["authorization"] == "Bearer s3cret"


async def test_credentials_stay_stripped_after_returning_to_original_origin():
    seen: list[httpx.Request] = []
    hops = iter(["https://other.example.org/a", "https://mcp.example.com/b"])
    state = {"n": 0}

    def redirect(request: httpx.Request):
        if state["n"] < 2:
            state["n"] += 1
            return httpx.Response(307, headers={"location": next(hops)})
        return None

    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=redirect),
        resolver=static_resolver(
            {"mcp.example.com": [PUBLIC], "other.example.org": ["93.184.216.35"]}
        ),
        headers=SECRET,
    )
    await c.list_tools()
    assert "authorization" in seen[0].headers
    assert "authorization" not in seen[1].headers
    assert "authorization" not in seen[2].headers


async def test_redirect_to_metadata_ip_rejected():
    seen: list[httpx.Request] = []
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(
            seen, redirect=_redirect_once("https://169.254.169.254/latest/meta-data/")
        ),
        headers=SECRET,
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert len(seen) == 1  # the metadata address was never contacted


async def test_redirect_hostname_is_resolved_and_revalidated_per_hop():
    seen: list[httpx.Request] = []
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=_redirect_once("https://rebind.example.org/x")),
        resolver=static_resolver(
            {"mcp.example.com": [PUBLIC], "rebind.example.org": ["192.168.1.10"]}
        ),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert len(seen) == 1


async def test_redirect_downgrade_to_http_rejected():
    seen: list[httpx.Request] = []
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=_redirect_once("http://mcp.example.com/mcp")),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert len(seen) == 1


async def test_redirect_loopback_via_public_host_cannot_reenter_loopback():
    """Allowlisted loopback → public → allowlisted loopback must stop at the return hop."""
    seen: list[httpx.Request] = []
    hops = {"n": 0}

    def redirect(request: httpx.Request):
        hops["n"] += 1
        if hops["n"] == 1:
            return httpx.Response(307, headers={"location": "https://public.example/mcp"})
        if hops["n"] == 2:
            return httpx.Response(307, headers={"location": "http://127.0.0.1:8000/mcp"})
        return None

    c = client(
        "http://127.0.0.1:8000/mcp",
        handler=mcp_handler(seen, redirect=redirect),
        resolver=static_resolver({"public.example": [PUBLIC]}),
        policy=RemotePolicy(loopback_allowlist=frozenset({"127.0.0.1"})),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert len(seen) == 2


async def test_redirect_from_public_host_to_allowlisted_loopback_rejected():
    seen: list[httpx.Request] = []
    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=_redirect_once("http://127.0.0.1:8000/mcp")),
        policy=RemotePolicy(loopback_allowlist=frozenset({"127.0.0.1"})),
    )
    with pytest.raises(SsrfError):
        await c.list_tools()
    assert len(seen) == 1


async def test_redirect_loop_is_bounded():
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request):
        return httpx.Response(307, headers={"location": "https://mcp.example.com/mcp"})

    c = client(
        "https://mcp.example.com/mcp",
        handler=mcp_handler(seen, redirect=redirect),
        policy=RemotePolicy(max_redirects=3),
    )
    with pytest.raises(PortunusError) as exc:
        await c.list_tools()
    assert exc.value.code == "upstream_crash"
    assert len(seen) == 4


# ---- upstream behaviour mapping ----------------------------------------------------------


async def test_401_maps_to_auth_required():
    c = client("https://mcp.example.com/mcp", handler=lambda r: httpx.Response(401))
    with pytest.raises(PortunusError) as exc:
        await c.list_tools()
    assert exc.value.code == "auth_required"


async def test_oversized_response_rejected():
    c = client(
        "https://mcp.example.com/mcp",
        handler=lambda r: httpx.Response(200, content=b"x" * 2048),
        policy=RemotePolicy(max_response_bytes=1024),
    )
    with pytest.raises(PortunusError) as exc:
        await c.list_tools()
    assert exc.value.code == "payload_too_large"


async def test_timeout_maps_to_upstream_timeout():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    c = client("https://mcp.example.com/mcp", handler=slow, policy=RemotePolicy(timeout_s=0.2))
    with pytest.raises(PortunusError) as exc:
        await c.list_tools()
    assert exc.value.code == "upstream_timeout"


async def test_errors_do_not_leak_header_values():
    c = client(
        "https://mcp.example.com/mcp",
        handler=lambda r: httpx.Response(500, text="Bearer s3cret"),
        headers=SECRET,
    )
    with pytest.raises(PortunusError) as exc:
        await c.list_tools()
    assert "s3cret" not in str(exc.value)


# ---- real loopback server: allowlisted http succeeds -------------------------------------


@contextlib.asynccontextmanager
async def loopback_mcp_server(seen_headers: list[dict[str, str]]):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method = lines[0].split()[0]
            hdrs = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ": " in ln)}
            seen_headers.append(hdrs)
            body = await reader.readexactly(int(hdrs.get("content-length", "0")))
            status, payload, extra = "200 OK", b"", ""
            if method == "POST":
                msg = json.loads(body)
                mid = msg.get("id")
                if mid is None:
                    status = "202 Accepted"
                else:
                    if msg["method"] == "server/discover":
                        payload = json.dumps(
                            {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601}}
                        ).encode()
                    else:
                        if msg["method"] == "initialize":
                            result: dict = {"protocolVersion": "2025-03-26"}
                            extra = "Mcp-Session-Id: loop-1\r\n"
                        elif msg["method"] == "tools/list":
                            result = {"tools": TOOLS}
                        else:
                            result = {"content": [{"type": "text", "text": "pong"}]}
                        payload = json.dumps(
                            {"jsonrpc": "2.0", "id": mid, "result": result}
                        ).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{extra}"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def test_allowlisted_loopback_http_probe_and_invoke_succeed():
    seen: list[dict[str, str]] = []
    async with loopback_mcp_server(seen) as port:
        policy = RemotePolicy(loopback_allowlist=frozenset({"127.0.0.1"}))
        c = RemoteClient(
            f"http://127.0.0.1:{port}/mcp", headers={"X-Api-Key": "k"}, policy=policy
        )
        assert await c.list_tools() == TOOLS
        out = await c.call_tool("alpha", {})
        assert out["content"][0]["text"] == "pong"
    assert seen and all(h.get("x-api-key") == "k" for h in seen)
    assert seen[0]["host"] == f"127.0.0.1:{port}"


async def test_loopback_http_denied_with_real_server_when_not_allowlisted():
    seen: list[dict[str, str]] = []
    async with loopback_mcp_server(seen) as port:
        c = RemoteClient(f"http://127.0.0.1:{port}/mcp", headers={}, policy=RemotePolicy())
        with pytest.raises(SsrfError):
            await c.list_tools()
    assert seen == []


def test_policy_from_env(monkeypatch):
    monkeypatch.setenv("PORTUNUS_REMOTE_LOOPBACK_ALLOWLIST", " 127.0.0.1 , localhost:8000,")
    p = RemotePolicy.from_env()
    assert p.loopback_allowlist == frozenset({"127.0.0.1", "localhost:8000"})
    monkeypatch.delenv("PORTUNUS_REMOTE_LOOPBACK_ALLOWLIST")
    assert RemotePolicy.from_env().loopback_allowlist == frozenset()


# ---- vault-backed proxy ------------------------------------------------------------------


def _vault() -> Vault:
    return Vault({1: os.urandom(32)})


async def _make_remote_server(vault: Vault, url: str, headers: dict[str, str], kind="remote"):
    server = Server(id=uuid.uuid4(), name="r", slug=f"r-{uuid.uuid4().hex[:6]}", kind=kind, url=url)
    async with async_session() as s:
        s.add(server)
        await s.flush()
        for name, value in headers.items():
            eid = uuid.uuid4()
            s.add(
                VaultEntry(
                    id=eid,
                    server_id=server.id,
                    name=name,
                    ciphertext=vault.seal(value.encode(), vault_entry_id=str(eid), key_version=1),
                    key_version=1,
                )
            )
        await s.commit()
    return server.id


async def test_proxy_decrypts_vault_headers_for_probe_and_invoke(db):
    vault = _vault()
    seen: list[httpx.Request] = []
    sid = await _make_remote_server(
        vault, "https://mcp.example.com/mcp", {"Authorization": "Bearer from-vault"}
    )
    proxy = RemoteProxy(
        async_session,
        vault,
        policy=RemotePolicy(),
        transport=httpx.MockTransport(mcp_handler(seen)),
        resolver=static_resolver({"mcp.example.com": [PUBLIC]}),
    )
    assert await proxy.probe(sid) == TOOLS
    out = await proxy.invoke(sid, "alpha", {"q": 1})
    assert json.loads(out["content"][0]["text"])["arguments"] == {"q": 1}
    assert seen and all(r.headers["authorization"] == "Bearer from-vault" for r in seen)


async def test_proxy_rejects_ssrf_url_stored_on_server(db):
    vault = _vault()
    seen: list[httpx.Request] = []
    sid = await _make_remote_server(vault, "https://169.254.169.254/mcp", {})
    proxy = RemoteProxy(
        async_session,
        vault,
        policy=RemotePolicy(),
        transport=httpx.MockTransport(mcp_handler(seen)),
    )
    with pytest.raises(SsrfError):
        await proxy.probe(sid)
    assert seen == []


async def test_proxy_refuses_stdio_server_and_unknown_server(db):
    vault = _vault()
    proxy = RemoteProxy(async_session, vault, policy=RemotePolicy())
    stdio = Server(id=uuid.uuid4(), name="s", slug="s1", kind="stdio", command="npx", args=[])
    async with async_session() as s:
        s.add(stdio)
        await s.commit()
    with pytest.raises(RegistryError) as exc:
        await proxy.probe(stdio.id)
    assert exc.value.code == "unsupported"
    with pytest.raises(RegistryError) as exc:
        await proxy.probe(uuid.uuid4())
    assert exc.value.status == 404
