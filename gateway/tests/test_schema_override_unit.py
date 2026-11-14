"""Schema override helpers and Quark -104 messaging (no Unix runner required)."""

from __future__ import annotations

import uuid

import pytest
from portunus_gateway.db.models import Tool
from portunus_gateway.omni import finish_result
from portunus_gateway.registry import (
    RegistryError,
    effective_schema,
    schema_hash,
    tool_view,
    validate_schema_override,
)


def test_effective_schema_prefers_override():
    t = Tool(
        id=uuid.uuid4(),
        server_id=uuid.uuid4(),
        upstream_name="cal",
        description="d",
        schema={"type": "object", "properties": {"query": {"type": "object"}}},
        schema_hash="probed",
        schema_override={"type": "object", "required": ["query"]},
        enabled=True,
    )
    assert effective_schema(t) == {"type": "object", "required": ["query"]}
    view = tool_view(t)
    assert view["schema_overridden"] is True
    assert view["schema"] == t.schema_override
    assert view["probed_schema"] == t.schema
    assert view["schema_hash"] == "probed"  # hash column as stored; callers rehash on set


def test_tool_view_without_override():
    probed = {"type": "object"}
    t = Tool(
        id=uuid.uuid4(),
        server_id=uuid.uuid4(),
        upstream_name="t",
        description=None,
        schema=probed,
        schema_hash=schema_hash(probed),
        schema_override=None,
        enabled=True,
    )
    view = tool_view(t)
    assert view["schema_overridden"] is False
    assert view["schema"] == probed
    assert "probed_schema" not in view


def test_validate_schema_override_rejects_non_object():
    with pytest.raises(RegistryError) as ei:
        validate_schema_override(["not", "an", "object"])
    assert ei.value.status == 422


def test_validate_schema_override_rejects_invalid_json_schema():
    with pytest.raises(RegistryError) as ei:
        validate_schema_override({"type": "objekt"})
    assert ei.value.status == 422
    assert ei.value.code == "invalid_schema"


def test_finish_result_quark_minus_104_message():
    quark = finish_result(
        {
            "content": [
                {
                    "type": "text",
                    "text": '{"exit_code":1,"code":-104,"message":"无法识别当前 Agent 环境"}',
                }
            ],
            "structuredContent": {
                "result": '{"exit_code":1,"code":-104,"message":"无法识别当前 Agent 环境"}'
            },
        },
        server="quark",
        tool="get_user_info",
    )
    assert quark.is_error
    err = quark.structured_content["error"]
    assert err["code"] == "upstream_error"
    assert "无法识别" in err["message"]
    assert err["upstream"].get("code") == -104


def test_finish_result_quark_nested_result_object():
    """Probe shape: exit_code + result object with code/msg."""
    quark = finish_result(
        {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"exit_code":1,"result":{"code":-104,'
                        '"msg":"无法识别当前 Agent 环境，禁止继续使用","action":"runtime"}}'
                    ),
                }
            ],
            "structuredContent": {
                "exit_code": 1,
                "result": {
                    "code": -104,
                    "msg": "无法识别当前 Agent 环境，禁止继续使用",
                    "action": "runtime",
                },
            },
        },
        server="quark",
        tool="browse",
    )
    assert quark.is_error
    assert quark.structured_content["error"]["upstream"].get("code") == -104
    assert "无法识别" in quark.structured_content["error"]["message"]


def test_finish_result_nested_login_pending_with_exit_zero():
    """Guangyapan-style: exit_code 0 envelope + LOGIN_PENDING inside result."""
    pending = finish_result(
        {
            "content": [
                {
                    "type": "text",
                    "text": '{"exit_code":0,"result":{"code":"LOGIN_PENDING","logged_in":false}}',
                }
            ],
            "structuredContent": {
                "exit_code": 0,
                "result": {"code": "LOGIN_PENDING", "logged_in": False},
            },
        },
        server="guangyapan",
        tool="account_rights",
    )
    assert pending.is_error
    assert pending.structured_content["error"]["code"] == "auth_required"


def test_finish_result_login_required_with_nonzero_exit():
    """Guangyapan list/search: exit_code 1 + LOGIN_REQUIRED must be auth_required."""
    required = finish_result(
        {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"exit_code":1,"result":{"code":"LOGIN_REQUIRED",'
                        '"logged_in":false,"data":{"message":"请先登录"}}}'
                    ),
                }
            ],
            "structuredContent": {
                "exit_code": 1,
                "result": {
                    "code": "LOGIN_REQUIRED",
                    "logged_in": False,
                    "data": {"message": "请先登录"},
                },
            },
        },
        server="guangyapan",
        tool="list",
    )
    assert required.is_error
    assert required.structured_content["error"]["code"] == "auth_required"
    assert "请先登录" in required.structured_content["error"]["message"]
