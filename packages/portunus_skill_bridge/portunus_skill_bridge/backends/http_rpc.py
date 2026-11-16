"""HTTP RPC backend: POST JSON envelope with dispatch field (WeRead-style)."""

from __future__ import annotations

from typing import Any

import httpx

from portunus_skill_bridge.backends.base import resolve_auth_headers
from portunus_skill_bridge.manifest import Manifest, ToolSpec


def _wire_arguments(tool: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Map MCP param names to vendor wire keys via FieldSpec.wire."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        spec = tool.input.get(key)
        wire = spec.wire if spec is not None and spec.wire else key
        out[wire] = value
    return out


class HttpRpcBackend:
    def __init__(self, manifest: Manifest) -> None:
        if manifest.http is None:
            raise ValueError("http_rpc requires http config")
        self.manifest = manifest
        self.http = manifest.http

    async def call(self, tool: ToolSpec, arguments: dict[str, Any]) -> Any:
        wired = _wire_arguments(tool, arguments)
        if self.http.dispatch_mode == "url_path":
            path = (tool.dispatch or "").lstrip("/")
            url = f"{self.http.base_url.rstrip('/')}/{path}"
            # Arguments first so manifest const keys cannot be overridden.
            flat: dict[str, Any] = {**wired, **self.http.const}
        else:
            url = self.http.base_url
            flat = {
                **wired,
                **self.http.const,
                self.http.dispatch_field: tool.dispatch,
            }
        if self.http.body_envelope == "params_comm":
            # QQ Music: business args under params; const (skill_version) under comm.
            body: dict[str, Any] = {"params": dict(wired), "comm": dict(self.http.const)}
        else:
            body = flat
        headers = {
            "Content-Type": "application/json",
            **(self.http.headers or {}),
            **resolve_auth_headers(self.manifest),
        }
        async with httpx.AsyncClient(timeout=self.http.timeout_s) as client:
            resp = await client.request(
                self.http.method,
                url,
                json=body,
                headers=headers,
            )
        # Return structured JSON when possible; otherwise text + status.
        ct = resp.headers.get("content-type", "").lower()
        if "json" in ct:
            try:
                data = resp.json()
            except ValueError:
                data = {"status_code": resp.status_code, "text": resp.text}
        else:
            data = {"status_code": resp.status_code, "text": resp.text}
        if not resp.is_success:
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error": data,
            }
        return data
