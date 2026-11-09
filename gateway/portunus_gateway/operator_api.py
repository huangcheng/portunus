"""Operator REST API: servers and tools (spec §8 dashboard back end).

Operator authentication is a **stub that denies everything** until the session/CSRF task wires
the real dependency; tests (and that task) replace ``require_operator`` through
``app.dependency_overrides``. It returns the operator identity recorded in the audit log.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .registry import Registry, RegistryError

SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$"


async def require_operator() -> str:
    """Placeholder operator auth: fail closed. Overridden in tests; replaced by the real
    session + CSRF dependency in the operator-auth task."""
    raise RegistryError(401, "unauthorized", "operator authentication is not configured")


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


class CreateServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: Literal["stdio", "remote"]
    command: str | None = Field(default=None, max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=64)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2048)


class EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


router = APIRouter(prefix="/api")


@router.post("/servers", status_code=201)
async def create_server(
    body: CreateServer,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.create_server(
        name=body.name,
        slug=body.slug,
        kind=body.kind,
        command=body.command,
        args=body.args,
        env=body.env,
        actor=actor,
    )


@router.get("/servers")
async def list_servers(
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> list[dict[str, Any]]:
    return await registry.list_servers()


@router.get("/servers/{server_id}/tools")
async def list_tools(
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> list[dict[str, Any]]:
    return await registry.list_tools(server_id)


@router.post("/servers/{server_id}/probe")
async def probe_server(
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.probe(server_id, actor)


@router.post("/servers/{server_id}/enable")
async def enable_server(
    server_id: uuid.UUID,
    body: EnableBody | None = None,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.set_server_enabled(server_id, (body or EnableBody()).enabled, actor)


@router.post("/tools/{tool_id}/enable")
async def enable_tool(
    tool_id: uuid.UUID,
    body: EnableBody | None = None,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.set_tool_enabled(tool_id, (body or EnableBody()).enabled, actor)


def create_app(registry: Registry) -> FastAPI:
    app = FastAPI(title="Portunus gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    app.include_router(router)

    @app.exception_handler(RegistryError)
    async def _registry_error(_request: Request, exc: RegistryError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "message": exc.message}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths/types only: never echo submitted values (env literals are secrets).
        errors = [
            {"loc": [str(p) for p in e["loc"]], "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content={"code": "invalid_request", "errors": errors}
        )

    return app
