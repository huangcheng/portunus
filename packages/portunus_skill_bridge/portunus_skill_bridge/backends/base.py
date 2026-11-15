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
        token = value if value.lower().startswith("bearer ") else f"{auth.prefix}{value}".strip()
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
        headers[header] = companion
    return headers
