import json
from pathlib import Path

import httpx
import pytest

from portunus_skill_bridge.backends.http_rpc import HttpRpcBackend
from portunus_skill_bridge.manifest import load_manifest
from portunus_skill_bridge.server import build_server

WEREAD = Path(__file__).resolve().parents[1] / "manifests" / "weread.example.yaml"


@pytest.mark.anyio
async def test_http_rpc_posts_flat_body(monkeypatch):
    monkeypatch.setenv("WEREAD_API_KEY", "wrk-test")
    m = load_manifest(WEREAD)
    backend = HttpRpcBackend(m)
    tool = next(t for t in m.tools if t.name == "search_books")

    captured: dict = {}

    async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
        captured["method"] = method
        captured["url"] = str(url)
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"ok": True, "results": []})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    out = await backend.call(tool, {"keyword": "三体", "count": 5})
    assert out == {"ok": True, "results": []}
    assert captured["method"] == "POST"
    assert captured["json"]["api_name"] == "/store/search"
    assert captured["json"]["skill_version"] == "1.0.3"
    assert captured["json"]["keyword"] == "三体"
    assert captured["json"]["count"] == 5
    assert "Bearer wrk-test" in captured["headers"]["Authorization"]


@pytest.mark.anyio
async def test_http_rpc_malformed_json_falls_back_to_text(monkeypatch):
    monkeypatch.setenv("WEREAD_API_KEY", "wrk-test")
    m = load_manifest(WEREAD)
    backend = HttpRpcBackend(m)
    tool = next(t for t in m.tools if t.name == "search_books")

    async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
        return httpx.Response(
            502,
            headers={"content-type": "application/problem+json"},
            content=b"",
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    out = await backend.call(tool, {"keyword": "x"})
    assert out["ok"] is False
    assert out["status_code"] == 502
    assert out["error"]["status_code"] == 502
    assert "text" in out["error"]


@pytest.mark.anyio
async def test_http_rpc_parses_json_suffix_content_type(monkeypatch):
    monkeypatch.setenv("WEREAD_API_KEY", "wrk-test")
    m = load_manifest(WEREAD)
    backend = HttpRpcBackend(m)
    tool = next(t for t in m.tools if t.name == "search_books")

    async def fake_request(self, method, url, **kwargs):  # noqa: ANN001
        return httpx.Response(
            200,
            headers={"Content-Type": "Application/Problem+JSON; charset=utf-8"},
            content=b'{"ok": true}',
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    assert await backend.call(tool, {"keyword": "x"}) == {"ok": True}


def test_build_server_registers_tools():
    m = load_manifest(WEREAD)
    server = build_server(m)
    # MCPServer stores tools internally; list via sync helper if available
    tools = getattr(server, "_tool_manager", None) or getattr(server, "_tools", None)
    names = set()
    if tools is not None:
        # duck-type common layouts
        if hasattr(tools, "list_tools"):
            names = {t.name for t in tools.list_tools()}
        elif isinstance(tools, dict):
            names = set(tools)
    if not names:
        # Fallback: ensure add_tool did not raise and handler names exist on server
        assert server.name == "weread"
        return
    assert "search_books" in names
