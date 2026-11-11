"""Gateway ASGI app: operator REST API (``/api``) plus the Omni MCP front door (``/mcp``).

The caller owns the long-lived pieces (runner client, slot manager, registry, authz store) and
passes them in; this module only assembles the HTTP surface and ties the MCP session manager to
the app lifespan.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from starlette.routing import Route

from .omni import Omni
from .operator_api import create_app
from .operator_auth import OperatorAuth
from .protected_resource import protected_resource_document
from .registry import Registry


def build_app(*, registry: Registry, omni: Omni, auth: OperatorAuth | None = None) -> FastAPI:
    app = create_app(registry, auth=auth)
    app.state.omni = omni
    # A Route (not a Mount) so ``POST /mcp`` matches without a trailing-slash redirect.
    app.router.routes.append(Route("/mcp", endpoint=omni.asgi))

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    async def oauth_protected_resource(request: Request) -> dict[str, Any]:
        """RFC 9728 Protected Resource Metadata (MCP authorization MUST when authenticating)."""
        headers = {k.lower(): v for k, v in request.headers.items()}
        return protected_resource_document(headers)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with omni.manager.run():
            yield

    app.router.lifespan_context = lifespan
    return app
