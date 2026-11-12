"""OAuth 2.0 Protected Resource Metadata (RFC 9728) for the ``/mcp`` endpoint.

Portunus issues operator-managed bearer API keys (``pm-...``), not an OAuth
authorization-server flow. The document still exists so standards-following
clients can discover *how* to authenticate: Bearer header, no AS listed,
documentation points at the operator dashboard.

``authorization_servers`` is intentionally absent (deliberate deviation from the MCP
authorization spec, which requires it only for servers implementing the OAuth framework —
MCP 2026-07-28 allows custom credential strategies): there is no AS to list, and RFC 9728
itself marks the field optional. See SECURITY.md "MCP authorization posture".
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlparse


def _origin_from_headers(headers: Mapping[str, str]) -> str | None:
    public = os.environ.get("PORTUNUS_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        parsed = urlparse(public)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").strip()
    if not host:
        return None
    proto = (headers.get("x-forwarded-proto") or "https").split(",")[0].strip() or "https"
    return f"{proto}://{host}"


def public_mcp_resource(headers: Mapping[str, str] | None = None) -> str | None:
    """Absolute URI of the MCP protected resource, or ``None`` when unknown."""
    headers = headers or {}
    public = os.environ.get("PORTUNUS_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        return public if public.endswith("/mcp") else f"{public}/mcp"
    origin = _origin_from_headers(headers)
    return f"{origin}/mcp" if origin else None


def resource_metadata_url(headers: Mapping[str, str] | None = None) -> str | None:
    headers = headers or {}
    public = os.environ.get("PORTUNUS_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        base = public[: -len("/mcp")] if public.endswith("/mcp") else public
        return f"{base}/.well-known/oauth-protected-resource"
    origin = _origin_from_headers(headers)
    return f"{origin}/.well-known/oauth-protected-resource" if origin else None


def bearer_challenge_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """RFC 6750 ``WWW-Authenticate`` with optional ``resource_metadata`` (RFC 9728)."""
    meta = resource_metadata_url(headers)
    if meta:
        return {
            "WWW-Authenticate": f'Bearer realm="portunus", resource_metadata="{meta}"'
        }
    return {"WWW-Authenticate": 'Bearer realm="portunus"'}


def protected_resource_document(headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Minimal RFC 9728 document for ``GET /.well-known/oauth-protected-resource``."""
    headers = headers or {}
    resource = public_mcp_resource(headers) or "portunus"
    doc_url = resource[: -len("/mcp")] + "/" if resource.endswith("/mcp") else resource
    return {
        "resource": resource,
        "bearer_methods_supported": ["header"],
        "resource_documentation": doc_url,
    }
