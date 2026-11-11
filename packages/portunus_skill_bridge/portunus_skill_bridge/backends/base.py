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
    if auth.type == "bearer_env":
        token = value if value.lower().startswith("bearer ") else f"{auth.prefix}{value}".strip()
        return {auth.header: token}
    if auth.type == "header_env":
        return {auth.header: value}
    raise RuntimeError(f"unsupported auth.type {auth.type!r}")
