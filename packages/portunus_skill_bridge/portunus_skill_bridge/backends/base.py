from __future__ import annotations

from typing import Any, Protocol

from portunus_skill_bridge.manifest import Manifest, ToolSpec


class Backend(Protocol):
    async def call(self, tool: ToolSpec, arguments: dict[str, Any]) -> Any: ...


def resolve_auth_headers(manifest: Manifest) -> dict[str, str]:
    """Build HTTP headers from env-backed auth (secrets injected by Portunus vault)."""
    import os

    auth = manifest.auth
    if auth.type in ("none",) or auth.is_platform_auth():
        return {}
    if not auth.env:
        raise RuntimeError(f"auth.type={auth.type} requires auth.env")
    value = os.environ.get(auth.env, "").strip()
    if not value:
        raise RuntimeError(
            f"missing secret env {auth.env!r}; connect the app in Portunus first"
        )
    headers: dict[str, str]
    if auth.type == "bearer_env":
        prefix = auth.prefix
        # Match the configured scheme, not a hard-coded "bearer ". Do not strip
        # the concatenation: a prefix without surrounding whitespace must survive.
        already = bool(prefix) and value.casefold().startswith(prefix.casefold())
        token = value if already else f"{prefix}{value}"
        headers = {auth.header: token}
    elif auth.type == "header_env":
        headers = {auth.header: value}
    else:
        raise RuntimeError(f"unsupported auth.type {auth.type!r}")
    for header, env_name in (auth.extra_headers_env or {}).items():
        companion = os.environ.get(env_name, "").strip()
        if not companion:
            raise RuntimeError(
                f"missing secret env {env_name!r}; connect the app in Portunus first"
            )
        # Header names are case-insensitive. A companion that reuses auth.header
        # would overwrite the token, or send two conflicting copies.
        collision = next((name for name in headers if name.lower() == header.lower()), None)
        if collision is not None:
            raise RuntimeError(f"extra header {header!r} collides with {collision!r}")
        headers[header] = companion
    return headers
