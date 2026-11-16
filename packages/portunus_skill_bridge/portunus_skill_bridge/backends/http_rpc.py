"""HTTP RPC backend: POST JSON envelope with dispatch field (WeRead-style)."""

from __future__ import annotations

from typing import Any

import httpx

from portunus_skill_bridge.backends.base import resolve_auth_headers
from portunus_skill_bridge.manifest import Manifest, ToolSpec


class HttpRpcBackend:
    def __init__(self, manifest: Manifest) -> None:
        if manifest.http is None:
            raise ValueError("http_rpc requires http config")
        self.manifest = manifest
        self.http = manifest.http

    async def call(self, tool: ToolSpec, arguments: dict[str, Any]) -> Any:
        if self.http.dispatch_mode == "url_path":
            path = (tool.dispatch or "").lstrip("/")
            url = f"{self.http.base_url.rstrip('/')}/{path}"
            # Arguments first so manifest const keys cannot be overridden.
            body: dict[str, Any] = {**arguments, **self.http.const}
        else:
            url = self.http.base_url
            body = {
                **arguments,
                **self.http.const,
                self.http.dispatch_field: tool.dispatch,
            }
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
