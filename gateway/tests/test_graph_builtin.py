"""First-party Microsoft Graph adapter (portunus-graph://)."""

from __future__ import annotations

import pytest

from portunus_gateway.graph_builtin import (
    GraphBuiltinClient,
    is_graph_builtin_url,
    parse_graph_builtin_url,
)
from portunus_gateway.registry import validate_remote_url
from portunus_shared import PortunusError

pytestmark = pytest.mark.anyio


def test_parse_graph_builtin_url():
    assert parse_graph_builtin_url("portunus-graph://mail") == "mail"
    assert parse_graph_builtin_url("portunus-graph://outlook-mail") == "mail"
    assert parse_graph_builtin_url("portunus-graph://onedrive") == "onedrive"
    assert parse_graph_builtin_url("https://graph.microsoft.com") is None
    with pytest.raises(PortunusError):
        parse_graph_builtin_url("portunus-graph://unknown")


def test_validate_remote_url_accepts_graph_scheme():
    assert validate_remote_url("portunus-graph://mail") == "portunus-graph://mail"
    assert validate_remote_url("portunus-graph://onedrive") == "portunus-graph://onedrive"
    with pytest.raises(Exception):
        validate_remote_url("portunus-graph://nope")


async def test_list_tools_without_auth():
    assert is_graph_builtin_url("portunus-graph://mail")
    client = GraphBuiltinClient("portunus-graph://mail", headers={})
    tools = await client.list_tools()
    names = {t["name"] for t in tools}
    assert "list_messages" in names and "send_mail" in names
    with pytest.raises(PortunusError) as ei:
        await client.call_tool("list_messages", {})
    assert ei.value.code == "auth_required"


async def test_list_tools_with_bad_bearer_raises():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://graph.microsoft.com/v1.0/me")
        return httpx.Response(401, text="unauthorized")

    client = GraphBuiltinClient(
        "portunus-graph://mail",
        headers={"Authorization": "Bearer dead"},
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PortunusError) as ei:
        await client.list_tools()
    assert ei.value.code == "auth_required"


async def test_onedrive_tool_catalog():
    # No vaulted bearer → static catalog only (pre-Connect preview).
    client = GraphBuiltinClient("portunus-graph://onedrive", headers={})
    tools = await client.list_tools()
    names = {t["name"] for t in tools}
    assert "list_children" in names and "upload_text" in names
