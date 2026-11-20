"""First-party JMAP adapter (portunus-jmap://)."""

from __future__ import annotations

import pytest

from portunus_gateway.jmap_builtin import (
    JmapBuiltinClient,
    is_jmap_builtin_url,
    parse_jmap_builtin_url,
)
from portunus_gateway.registry import validate_remote_url
from portunus_shared import PortunusError

pytestmark = pytest.mark.anyio


def test_parse_jmap_builtin_url():
    assert parse_jmap_builtin_url("portunus-jmap://thundermail") == "thundermail"
    assert parse_jmap_builtin_url("https://mail.thundermail.com") is None
    with pytest.raises(PortunusError):
        parse_jmap_builtin_url("portunus-jmap://unknown")


def test_validate_remote_url_accepts_jmap_scheme():
    assert validate_remote_url("portunus-jmap://thundermail") == "portunus-jmap://thundermail"
    with pytest.raises(Exception):
        validate_remote_url("portunus-jmap://nope")


async def test_list_tools_without_auth():
    assert is_jmap_builtin_url("portunus-jmap://thundermail")
    client = JmapBuiltinClient("portunus-jmap://thundermail", headers={})
    tools = await client.list_tools()
    names = {t["name"] for t in tools}
    assert "list_messages" in names and "send_mail" in names
    with pytest.raises(PortunusError) as ei:
        await client.call_tool("list_messages", {})
    assert ei.value.code == "auth_required"
