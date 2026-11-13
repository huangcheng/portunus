"""Handler exceptions become CLI envelopes (not bare MCP 'Error executing tool')."""

from __future__ import annotations

import json

import pytest

from portunus_skill_bridge.manifest import ToolSpec
from portunus_skill_bridge.server import _make_handler, _params_model


@pytest.mark.anyio
async def test_handler_exception_returns_exit_code_envelope():
    tool = ToolSpec.model_validate(
        {"name": "list", "description": "List files", "operation": "list", "input": {}}
    )
    params = _params_model(tool)

    async def boom(_tool, _args):
        raise RuntimeError("cli.cwd does not exist: /missing")

    handler = _make_handler(tool, params, boom)
    raw = await handler()
    body = json.loads(raw)
    assert body["exit_code"] == 1
    assert "cli.cwd does not exist" in body["message"]
    assert body["error"] == "RuntimeError"
    assert "cli.cwd does not exist" in (body.get("stderr") or "")
