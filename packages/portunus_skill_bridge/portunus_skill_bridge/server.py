"""Build an MCP stdio server from a manifest."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, create_model

from portunus_skill_bridge.backends import build_backend
from portunus_skill_bridge.manifest import Manifest, ToolSpec, validate_input_field_name

log = logging.getLogger("portunus.skill_bridge")

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _params_model(tool: ToolSpec) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for name, spec in tool.input.items():
        validate_input_field_name(name)
        py_t = _TYPE_MAP.get(spec.type, Any)
        desc = spec.description or ""
        if spec.required:
            fields[name] = (py_t, Field(description=desc))
        else:
            fields[name] = (py_t | None, Field(default=None, description=desc))
    if not fields:
        # Empty model still works for zero-arg tools.
        return create_model(f"{tool.name}_Params")
    return create_model(f"{tool.name}_Params", **fields)


def _make_handler(
    tool: ToolSpec, params_model: type[BaseModel], call: Callable[..., Any]
) -> Callable[..., Any]:
    """Flat kwargs handler so MCP schema is tool fields, not a nested ``params`` object."""

    async def _handler(**kwargs: Any) -> str:
        try:
            data = params_model.model_validate(kwargs).model_dump(exclude_none=True)
            result = await call(tool, data)
        except Exception as e:
            # Prefer a CLI-shaped envelope over the MCP SDK's generic
            # "Error executing tool …" so the gateway can surface stderr/message.
            log.exception("skill-bridge tool %s failed", tool.name)
            envelope = {
                "exit_code": 1,
                "message": str(e)[:500] or type(e).__name__,
                "stderr": str(e)[:2000] or None,
                "error": type(e).__name__,
            }
            return json.dumps(envelope, ensure_ascii=False, default=str)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    _handler.__name__ = tool.name
    _handler.__doc__ = tool.description or tool.name

    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {"return": str}
    for name, finfo in params_model.model_fields.items():
        ann = finfo.annotation
        annotations[name] = ann
        if finfo.is_required():
            parameters.append(
                inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann)
            )
        else:
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=ann,
                )
            )
    _handler.__signature__ = inspect.Signature(parameters, return_annotation=str)
    _handler.__annotations__ = annotations
    return _handler


def build_server(manifest: Manifest) -> MCPServer:
    backend = build_backend(manifest)
    server = MCPServer(
        manifest.name,
        instructions=f"Portunus skill-bridge ({manifest.backend}) v{manifest.version}",
    )

    for tool in manifest.tools:
        params_model = _params_model(tool)
        handler = _make_handler(tool, params_model, backend.call)
        server.add_tool(
            handler,
            name=tool.name,
            description=tool.description or tool.name,
        )

    return server
