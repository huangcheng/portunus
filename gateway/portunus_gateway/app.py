"""Gateway ASGI app: operator REST API (``/api``) plus the Omni MCP front door (``/mcp``).

The caller owns the long-lived pieces (runner client, slot manager, registry, authz store) and
passes them in; this module only assembles the HTTP surface and ties the MCP session manager to
the app lifespan.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .cimd import CIMD_PATH, cimd_document
from .omni import Omni
from .operator_api import create_app
from .operator_auth import OperatorAuth
from .protected_resource import protected_resource_document
from .registry import Registry

_SKILL_MD = Path(__file__).resolve().parent / "static" / "SKILL.md"


def _load_skill_md() -> str | None:
    """Read packaged SKILL.md once; None when missing or unreadable."""
    try:
        if not _SKILL_MD.is_file():
            return None
        return _SKILL_MD.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


_SKILL_MD_TEXT = _load_skill_md()


def build_app(*, registry: Registry, omni: Omni, auth: OperatorAuth | None = None) -> FastAPI:
    app = create_app(registry, auth=auth)
    app.state.omni = omni
    # A Route (not a Mount) so ``POST /mcp`` matches without a trailing-slash redirect.
    app.router.routes.append(Route("/mcp", endpoint=omni.asgi))

    # Domain agent skill — registered before the SPA StaticFiles mount in main.py.
    @app.get("/SKILL.md", include_in_schema=False)
    async def agent_skill_md() -> Response:
        """Public agent instructions for connecting to this Portunus gateway."""
        if _SKILL_MD_TEXT is None:
            return JSONResponse(
                status_code=404,
                content={"code": "not_found", "message": "SKILL.md is not packaged"},
            )
        return PlainTextResponse(
            _SKILL_MD_TEXT,
            media_type="text/markdown; charset=utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get(CIMD_PATH, include_in_schema=False, response_model=None)
    async def oauth_client_metadata() -> dict[str, Any] | JSONResponse:
        """OAuth Client ID Metadata Document (CIMD) for MCP authorization."""
        doc = cimd_document()
        if doc is None:
            return JSONResponse(
                status_code=404,
                content={
                    "code": "not_found",
                    "message": "PORTUNUS_PUBLIC_URL is not configured for CIMD",
                },
            )
        return JSONResponse(
            content=doc,
            headers={"Cache-Control": "public, max-age=300"},
        )

    # RFC 9728 §3.1: resource ``<origin>/mcp`` is also discovered at the path-suffixed URI.
    # response_model=None: the union return is not a Pydantic field, and stacking
    # these decorators otherwise builds that model twice.
    @app.get(
        "/.well-known/oauth-protected-resource",
        include_in_schema=False,
        response_model=None,
    )
    @app.get(
        "/.well-known/oauth-protected-resource/mcp",
        include_in_schema=False,
        response_model=None,
    )
    async def oauth_protected_resource(request: Request) -> dict[str, Any] | JSONResponse:
        """RFC 9728 Protected Resource Metadata (MCP authorization MUST when authenticating)."""
        headers = {k.lower(): v for k, v in request.headers.items()}
        doc = protected_resource_document(headers)
        if doc is None:
            return JSONResponse(
                status_code=404,
                content={
                    "code": "not_found",
                    "message": "protected resource URL is not configured",
                },
            )
        return doc

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with omni.manager.run():
            yield

    app.router.lifespan_context = lifespan
    return app
