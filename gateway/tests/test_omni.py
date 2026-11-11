"""Omni ``/mcp`` (Task 10): four meta tools end-to-end with an agent key.

Real runner (dev mode) + fixture MCP server over stdio, real SlotManager / AuthzStore / keys,
MCP SDK client over Streamable HTTP against the ASGI app.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from portunus_gateway import __version__ as gateway_version
from portunus_gateway import keys
from portunus_gateway.app import build_app
from portunus_gateway.db.models import Grant, Tool
from portunus_gateway.db.session import async_session
from portunus_gateway.omni import MAX_RESULT_BYTES, Omni, ServerStateCache, finish_result
from portunus_gateway.registry import Registry
from portunus_gateway.slots import SlotManager
from test_registry_probe import _body, _vault, policy_dir, runner_with_fixture  # noqa: F401

pytestmark = pytest.mark.anyio

META = [
    "portunus_discover_servers",
    "portunus_discover_tools",
    "portunus_get_tool_schema",
    "portunus_invoke_tool",
]

BASE_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def settle(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


class Stack:
    def __init__(self, registry, slots, omni, app, authz, runner, srv):
        self.registry, self.slots, self.omni, self.app = registry, slots, omni, app
        self.authz, self.runner, self.srv = authz, runner, srv

    async def add_server(self, slug="fix", *, env=None, enable=True, tools=("alpha", "beta")):
        """Create + probe a fixture server; enable it and the named tools."""
        r = await self.registry.create_server(
            name=slug,
            slug=slug,
            kind="stdio",
            command="npx",
            args=_body()["args"],
            env=env or {},
            actor="test",
        )
        sid = uuid.UUID(r["id"])
        await self.registry.probe(sid, "test")
        if enable:
            await self.registry.set_server_enabled(sid, True, "test")
        return sid

    async def add_key(self, *servers):
        token, row = await keys.mint_agent_key(expires_at=None, label="t")
        async with async_session() as s:
            for sid in servers:
                s.add(Grant(key_id=row.key_id, server_id=sid))
            await s.commit()
        await self.authz.reload_from_db()
        return token, row.key_id

    @contextlib.asynccontextmanager
    async def client(self, token):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as hc:
            async with Client(streamable_http_client("http://test/mcp", http_client=hc)) as c:
                yield c


def make_stack_fixture(**omni_kw):
    @pytest.fixture
    async def stack(runner_with_fixture, policy_dir, db, authz, pepper_file):  # noqa: F811
        runner, srv = runner_with_fixture
        cache = ServerStateCache()
        slots = SlotManager(
            runner,
            authz,
            server_enabled=cache.enabled,
            vault_version=cache.vault_version,
            stop_retry_s=0.05,
        )
        slots.attach(runner)
        registry = Registry(
            async_session,
            _vault(),
            runner,
            trusted_path=[str(policy_dir / "bin"), "/usr/bin", "/bin"],
            binary_allowlist=[],
            slots=slots,
            probe_timeout_s=10,
        )
        omni = Omni(
            session_factory=async_session,
            registry=registry,
            slots=slots,
            runner=runner,
            authz=authz,
            state_cache=cache,
            **omni_kw,
        )
        app = build_app(registry=registry, omni=omni)
        async with app.router.lifespan_context(app):
            yield Stack(registry, slots, omni, app, authz, runner, srv)

    return stack


stack = make_stack_fixture()
fast_timeout_stack = make_stack_fixture(invoke_timeout_s=1)
rate_stack = make_stack_fixture(rate_per_min=2)


def payload(result):
    """Decode the JSON structured content / text of an Omni result."""
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def error_code(result):
    assert result.is_error, result
    return payload(result)["error"]["code"]


# ---- surface -----------------------------------------------------------------------


async def test_tools_list_only_four_meta(stack):
    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    async with stack.client(token) as c:
        tools = (await c.list_tools()).tools
    assert sorted(t.name for t in tools) == META
    for t in tools:  # upstream tools are never flattened into the list
        assert t.input_schema["type"] == "object"


def _guard_code(body: dict) -> str:
    """HTTP-guard failures use a JSON-RPC envelope with the Portunus code in ``data.code``."""
    err = body["error"]
    assert body.get("jsonrpc") == "2.0"
    assert isinstance(err["code"], int)
    return err["data"]["code"]


async def test_requires_bearer_agent_key(stack):
    sid = await stack.add_server()
    token, key_id = await stack.add_key(sid)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.app), base_url="http://test"
    ) as hc:
        for auth in (None, "Bearer nope", "Basic abc", f"Bearer {token[:-2]}xx"):
            h = dict(headers, **({"Authorization": auth} if auth else {}))
            r = await hc.post("/mcp", json=body, headers=h)
            assert r.status_code == 401, auth
            assert r.headers["www-authenticate"].startswith("Bearer")
            assert _guard_code(r.json()) == "unauthorized"
        await keys.revoke_key(key_id)
        r = await hc.post("/mcp", json=body, headers=dict(headers, Authorization=f"Bearer {token}"))
        assert r.status_code == 401 and _guard_code(r.json()) == "unauthorized"


async def test_oversize_request_body_rejected(stack):
    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    big = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"pad": "x" * (1 << 20)}}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.app), base_url="http://test"
    ) as hc:
        r = await hc.post(
            "/mcp",
            json=big,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert r.status_code == 413
    assert _guard_code(r.json()) == "payload_too_large"


async def test_initialize_reports_package_version(stack):
    """``serverInfo.version`` must be a real version, not the transport's blank default."""
    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.app), base_url="http://test"
    ) as hc:
        r = await hc.post(
            "/mcp", json=body, headers=BASE_HEADERS | {"Authorization": f"Bearer {token}"}
        )
    info = r.json()["result"]["serverInfo"]
    assert info["name"] == "portunus"
    assert info["version"] == gateway_version
    assert info["version"].strip()


async def test_batch_request_refused_without_leaking_internals(stack):
    """Batches are illegal in current MCP; refuse them cleanly instead of echoing a pydantic trace."""
    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.app), base_url="http://test"
    ) as hc:
        r = await hc.post(
            "/mcp", json=batch, headers=BASE_HEADERS | {"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600
    assert "batch" in r.json()["error"]["message"].lower()
    leaked = r.text.lower()
    for needle in ("pydantic", "validation error", "docs."):
        assert needle not in leaked, leaked


async def test_invalid_envelope_refused_without_leaking_internals(stack):
    """Malformed JSON-RPC objects must not echo a pydantic union dump (review #10)."""
    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    bad = [
        {"id": 1, "method": "ping"},  # missing jsonrpc
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": "oops"},
    ]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.app), base_url="http://test"
    ) as hc:
        for body in bad:
            r = await hc.post(
                "/mcp", json=body, headers=BASE_HEADERS | {"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 400, body
            assert r.json()["error"]["code"] == -32602
            assert r.json()["error"]["message"] == "invalid JSON-RPC envelope"
            leaked = r.text.lower()
            for needle in ("pydantic", "validation error", "docs.", "jsonrpcrequest"):
                assert needle not in leaked, leaked


async def test_auth_scheme_mistake_is_reported_precisely(stack):
    """A bare token is the common mistake; do not claim the request carried no key at all."""
    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.app), base_url="http://test"
    ) as hc:
        bare = await hc.post("/mcp", json=body, headers=BASE_HEADERS | {"Authorization": token})
        missing = await hc.post("/mcp", json=body, headers=BASE_HEADERS)
        empty = await hc.post("/mcp", json=body, headers=BASE_HEADERS | {"Authorization": "Bearer "})
    assert bare.status_code == 401
    assert bare.json()["error"]["message"] == "Authorization must use the Bearer scheme"
    assert missing.json()["error"]["message"] == "agent key required"
    assert empty.json()["error"]["message"] == "agent key required"
    for r in (bare, missing, empty):
        assert _guard_code(r.json()) == "unauthorized"
        assert r.headers["www-authenticate"].startswith("Bearer")


async def test_tool_calls_are_rate_limited_per_key(rate_stack):
    """The 60 calls/min/key limit is real; without this test a no-op limiter looks fine."""
    sid = await rate_stack.add_server()
    token, _ = await rate_stack.add_key(sid)
    async with rate_stack.client(token) as c:
        assert payload(await c.call_tool("portunus_discover_servers", {}))["items"]
        assert not (await c.call_tool("portunus_discover_servers", {})).is_error
        over = await c.call_tool("portunus_discover_servers", {})
    assert error_code(over) == "rate_limited"


# ---- discovery ---------------------------------------------------------------------


async def test_happy_path_discover_and_invoke(stack):
    sid = await stack.add_server()
    other = await stack.add_server("other")  # exists but is not granted
    token, _ = await stack.add_key(sid)
    async with stack.client(token) as c:
        servers = payload(await c.call_tool("portunus_discover_servers", {"query": ""}))
        assert [s["slug"] for s in servers["items"]] == ["fix"]  # ungranted server is invisible
        assert servers["next_cursor"] is None

        tools = payload(await c.call_tool("portunus_discover_tools", {"server": "fix"}))
        assert [t["name"] for t in tools["items"]] == ["alpha", "beta"]
        assert all(t["schema_hash"] for t in tools["items"])

        schema = payload(
            await c.call_tool("portunus_get_tool_schema", {"server": "fix", "tool": "alpha"})
        )
        assert schema["name"] == "alpha"
        assert schema["input_schema"]["properties"]["q"] == {"type": "string"}

        res = await c.call_tool(
            "portunus_invoke_tool",
            {"server": "fix", "tool": "alpha", "arguments": {"q": "hello"}},
        )
        assert not res.is_error, res
        assert json.loads(res.content[0].text) == {"q": "hello"}

        # same (server, key) pair reuses one child
        await c.call_tool(
            "portunus_invoke_tool", {"server": "fix", "tool": "alpha", "arguments": {"q": "again"}}
        )
        assert len(stack.slots.slots) == 1

        # an upstream isError result is passed through as a tool error, not masked
        res = await c.call_tool("portunus_invoke_tool", {"server": "fix", "tool": "beta"})
        assert res.is_error and res.content[0].text == "beta failed"
    assert other  # silence unused


async def test_discover_servers_filters_and_pages(stack):
    sids = [await stack.add_server(f"srv{i:02d}") for i in range(3)]
    hidden = await stack.add_server("disabled", enable=False)
    token, _ = await stack.add_key(*sids, hidden)
    async with stack.client(token) as c:
        r = payload(await c.call_tool("portunus_discover_servers", {"limit": 2}))
        assert [s["slug"] for s in r["items"]] == ["srv00", "srv01"] and r["next_cursor"]
        r2 = payload(
            await c.call_tool("portunus_discover_servers", {"limit": 2, "cursor": r["next_cursor"]})
        )
        assert [s["slug"] for s in r2["items"]] == ["srv02"] and r2["next_cursor"] is None
        q = payload(await c.call_tool("portunus_discover_servers", {"query": "SRV01"}))
        assert [s["slug"] for s in q["items"]] == ["srv01"]


async def test_discover_tools_cursor_pages_50_and_100(stack):
    sid = await stack.add_server()
    async with async_session() as s:
        for i in range(130):
            s.add(
                Tool(
                    server_id=sid,
                    upstream_name=f"t{i:03d}",
                    description="d",
                    schema={"type": "object"},
                    schema_hash="h",
                    enabled=True,
                )
            )
        await s.commit()
    token, _ = await stack.add_key(sid)
    async with stack.client(token) as c:

        async def pages(**kw):
            names, cursor, sizes = [], None, []
            while True:
                args = {"server": "fix", **kw, **({"cursor": cursor} if cursor else {})}
                r = payload(await c.call_tool("portunus_discover_tools", args))
                names += [t["name"] for t in r["items"]]
                sizes.append(len(r["items"]))
                cursor = r["next_cursor"]
                if not cursor:
                    return names, sizes

        names, sizes = await pages()
        assert sizes == [50, 50, 32] and len(set(names)) == 132  # default page: 50
        _, sizes = await pages(limit=100)
        assert sizes == [100, 32]
        bad = await c.call_tool("portunus_discover_tools", {"server": "fix", "limit": 101})
        assert error_code(bad) == "invalid_gateway_arguments"  # >100 refused, never silently clamped
        bad = await c.call_tool(
            "portunus_discover_tools", {"server": "fix", "cursor": "!!not-a-cursor"}
        )
        assert error_code(bad) == "invalid_cursor"


# ---- authorization and admission -----------------------------------------------------


async def test_invoke_requires_grant(stack):
    sid = await stack.add_server()
    token, _ = await stack.add_key()  # no grants
    async with stack.client(token) as c:
        servers = payload(await c.call_tool("portunus_discover_servers", {}))
        assert servers["items"] == []
        for name, args in (
            ("portunus_invoke_tool", {"server": "fix", "tool": "alpha"}),
            ("portunus_get_tool_schema", {"server": "fix", "tool": "alpha"}),
            ("portunus_discover_tools", {"server": "fix"}),
            ("portunus_invoke_tool", {"server": "no-such-server", "tool": "alpha"}),
        ):
            res = await c.call_tool(name, args)
            assert error_code(res) == "not_granted", name  # unknown == ungranted: no oracle
    assert stack.srv.children.empty()  # nothing was spawned for an unauthorized call
    assert sid


async def test_server_disabled_and_tool_missing(stack):
    sid = await stack.add_server()
    off = await stack.add_server("off", enable=False)
    token, _ = await stack.add_key(sid, off)
    async with stack.client(token) as c:
        assert error_code(
            await c.call_tool("portunus_invoke_tool", {"server": "off", "tool": "alpha"})
        ) == "server_disabled"
        assert error_code(
            await c.call_tool("portunus_get_tool_schema", {"server": "fix", "tool": "nope"})
        ) == "tool_missing"
        tools = payload(await c.call_tool("portunus_discover_tools", {"server": "fix"}))
        assert [t["name"] for t in tools["items"]] == ["alpha", "beta"]
    assert stack.srv.children.empty()


async def test_tool_disabled_hidden_and_blocked(stack):
    from sqlalchemy import select

    sid = await stack.add_server()
    token, _ = await stack.add_key(sid)
    async with async_session() as s:
        alpha = (
            await s.execute(
                select(Tool).where(Tool.server_id == sid, Tool.upstream_name == "alpha")
            )
        ).scalar_one()
        tool_id = alpha.id
    await stack.registry.set_tool_enabled(tool_id, False, "test")

    async with stack.client(token) as c:
        tools = payload(await c.call_tool("portunus_discover_tools", {"server": "fix"}))
        assert [t["name"] for t in tools["items"]] == ["beta"]
        servers = payload(await c.call_tool("portunus_discover_servers", {}))
        assert servers["items"][0]["tool_count"] == 1
        assert error_code(
            await c.call_tool("portunus_get_tool_schema", {"server": "fix", "tool": "alpha"})
        ) == "tool_disabled"
        assert error_code(
            await c.call_tool("portunus_invoke_tool", {"server": "fix", "tool": "alpha"})
        ) == "tool_disabled"
        # still-enabled tool remains visible / schema-fetchable
        schema = payload(
            await c.call_tool("portunus_get_tool_schema", {"server": "fix", "tool": "beta"})
        )
        assert schema["name"] == "beta"
    assert stack.srv.children.empty()


async def test_is_revoked_checked_before_has_grant(stack):
    sid = await stack.add_server()
    _token, key_id = await stack.add_key(sid)
    stack.authz.mark_revoked(key_id)
    assert stack.authz.has_grant(key_id, sid)  # grant rows of a revoked key linger
    res = await stack.omni.dispatch(
        key_id, "portunus_invoke_tool", {"server": "fix", "tool": "alpha"}
    )
    assert error_code(res) == "revoked"
    res = await stack.omni.dispatch(key_id, "portunus_discover_servers", {})
    assert error_code(res) == "revoked"
    assert stack.srv.children.empty()


async def test_schema_validation_and_pin(stack):
    sid = await stack.add_server("pinsrv")
    token, _ = await stack.add_key(sid)
    async with stack.client(token) as c:
        res = await c.call_tool(
            "portunus_invoke_tool",
            {"server": "pinsrv", "tool": "alpha", "arguments": {"q": 5}},
        )
        assert error_code(res) == "invalid_tool_arguments"
        assert stack.srv.children.empty()  # rejected before any child was started

        schema_res = await c.call_tool(
            "portunus_get_tool_schema", {"server": "pinsrv", "tool": "alpha"}
        )
        assert not schema_res.is_error, payload(schema_res)
        h = payload(schema_res)
        assert h.get("schema_pin") == "optional"
        ok = await c.call_tool(
            "portunus_invoke_tool",
            {
                "server": "pinsrv",
                "tool": "alpha",
                "arguments": {"q": "x"},
                "schema_hash": h["schema_hash"],
            },
        )
        assert not ok.is_error
        stale = await c.call_tool(
            "portunus_invoke_tool",
            {
                "server": "pinsrv",
                "tool": "alpha",
                "arguments": {"q": "x"},
                "schema_hash": "0" * 64,
            },
        )
        assert error_code(stale) == "schema_drift"


# ---- Review Focus: oversized result -> payload_too_large ------------------------------


async def test_oversized_result_is_payload_too_large_never_truncated_success(stack):
    sid = await stack.add_server(env={"FAKE_MCP_EXTRA": "1"}, tools=("alpha", "big"))
    token, _ = await stack.add_key(sid)
    async with stack.client(token) as c:
        res = await c.call_tool("portunus_invoke_tool", {"server": "fix", "tool": "big"})
        assert error_code(res) == "payload_too_large"
        assert "xxxx" not in json.dumps(res.model_dump(mode="json"))  # no partial payload leaks
        # a result at the cap is fine; the child is still usable afterwards
        ok = await c.call_tool(
            "portunus_invoke_tool", {"server": "fix", "tool": "alpha", "arguments": {"q": "y"}}
        )
        assert not ok.is_error


def test_result_cap_is_one_mib():
    assert MAX_RESULT_BYTES == 1024 * 1024
    empty = {"content": [{"type": "text", "text": ""}]}
    pad = MAX_RESULT_BYTES - len(json.dumps(empty, separators=(",", ":")))
    at_cap = finish_result({"content": [{"type": "text", "text": "x" * pad}]})
    assert not at_cap.is_error  # exactly at the cap passes
    over = finish_result({"content": [{"type": "text", "text": "x" * (pad + 1)}]})
    assert over.is_error and payload(over)["error"]["code"] == "payload_too_large"
    assert "xxxx" not in json.dumps(over.model_dump(mode="json"))


def test_finish_result_promotes_json_text_to_structured_content():
    """stdio peers often put JSON only in content[0].text (review #19)."""
    promoted = finish_result(
        {"content": [{"type": "text", "text": '{"path":"/","entries":["a"]}'}]}
    )
    assert not promoted.is_error
    assert promoted.structured_content == {"path": "/", "entries": ["a"]}
    assert promoted.content[0].text == '{"path":"/","entries":["a"]}'

    listed = finish_result({"content": [{"type": "text", "text": '["a","b"]'}]})
    assert listed.structured_content == {"items": ["a", "b"]}

    prose = finish_result({"content": [{"type": "text", "text": "Error reading file: 404"}]} )
    assert prose.structured_content is None

    kept = finish_result(
        {
            "content": [{"type": "text", "text": '{"ignored":true}'}],
            "structuredContent": {"account_id": "x"},
        }
    )
    assert kept.structured_content == {"account_id": "x"}

    # review #16: proxied isError prose gets a gateway envelope; text stays
    proxied = finish_result(
        {
            "content": [{"type": "text", "text": "Error listing directory: 404 Not Found"}],
            "isError": True,
        },
        server="koofr",
        tool="webdav_list_remote_directory",
    )
    assert proxied.is_error
    assert proxied.content[0].text == "Error listing directory: 404 Not Found"
    assert proxied.structured_content == {
        "error": {
            "code": "upstream_error",
            "message": "Error listing directory: 404 Not Found",
            "upstream": {"server": "koofr", "tool": "webdav_list_remote_directory"},
        }
    }
    # already-structured gateway errors are left alone
    already = finish_result(
        {
            "content": [{"type": "text", "text": '{"error":{"code":"auth_required","message":"x"}}'}],
            "structuredContent": {"error": {"code": "auth_required", "message": "x"}},
            "isError": True,
        }
    )
    assert already.structured_content == {"error": {"code": "auth_required", "message": "x"}}


# ---- timeout / concurrency / rate / revoke ------------------------------------------------


async def test_invoke_timeout_default_is_60s(stack):
    assert stack.omni.invoke_timeout_s == 60


async def test_invoke_timeout_kills_wedged_child(fast_timeout_stack):
    stack = fast_timeout_stack
    sid = await stack.add_server(env={"FAKE_MCP_EXTRA": "1"}, tools=("alpha", "slow"))
    token, _ = await stack.add_key(sid)
    async with stack.client(token) as c:
        res = await c.call_tool("portunus_invoke_tool", {"server": "fix", "tool": "slow"})
        assert error_code(res) == "upstream_timeout"
        # the wedged child is retired (pending_stop -> kill -> exit evidence), then work resumes
        await settle(lambda: stack.slots.slots == {} and stack.srv.children.empty())
        ok = await c.call_tool(
            "portunus_invoke_tool", {"server": "fix", "tool": "alpha", "arguments": {"q": "z"}}
        )
        assert not ok.is_error


async def test_concurrent_invoke_cap_per_key(fast_timeout_stack):
    stack = fast_timeout_stack
    stack.omni.max_concurrent = 1
    sid = await stack.add_server(env={"FAKE_MCP_EXTRA": "1"}, tools=("alpha", "slow"))
    token, key_id = await stack.add_key(sid)
    async with stack.client(token) as c:
        slow = asyncio.create_task(
            c.call_tool("portunus_invoke_tool", {"server": "fix", "tool": "slow"})
        )
        await settle(lambda: stack.omni.inflight(key_id) == 1)
        res = await c.call_tool(
            "portunus_invoke_tool", {"server": "fix", "tool": "alpha", "arguments": {}}
        )
        assert error_code(res) == "rate_limited"
        assert error_code(await slow) == "upstream_timeout"


async def test_rate_limit_per_key(stack):
    sid = await stack.add_server()
    stack.omni.rate_per_min = 3
    t1, _ = await stack.add_key(sid)
    t2, _ = await stack.add_key(sid)
    async with stack.client(t1) as c1, stack.client(t2) as c2:
        for _ in range(3):
            assert not (await c1.call_tool("portunus_discover_servers", {})).is_error
        assert error_code(await c1.call_tool("portunus_discover_servers", {})) == "rate_limited"
        assert not (await c2.call_tool("portunus_discover_servers", {})).is_error  # per key


async def test_revoke_during_invoke_never_returns_success(fast_timeout_stack):
    stack = fast_timeout_stack
    stack.omni.invoke_timeout_s = 30
    sid = await stack.add_server(env={"FAKE_MCP_EXTRA": "1"}, tools=("alpha", "slow"))
    token, key_id = await stack.add_key(sid)
    async with stack.client(token) as c:
        call = asyncio.create_task(
            c.call_tool("portunus_invoke_tool", {"server": "fix", "tool": "slow"})
        )
        await settle(lambda: not stack.srv.children.empty())
        await settle(lambda: stack.omni.inflight(key_id) == 1)
        stack.authz.mark_revoked(key_id)
        stack.slots.revoke_key(key_id)
        res = await asyncio.wait_for(call, 10)
        assert res.is_error
        assert error_code(res) in {"revoked", "cancelled", "upstream_crash", "runner_unavailable"}
    await settle(lambda: stack.slots.slots == {})
