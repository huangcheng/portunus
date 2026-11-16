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
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .origin import allowed_origins, normalize_origin

# Host / host:port only — no scheme, path, quotes, or whitespace.
_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})*"
    r"|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$"
)


def _origin_from_headers(headers: Mapping[str, str]) -> str | None:
    public = os.environ.get("PORTUNUS_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        try:
            parsed = urlparse(public)
        except ValueError:
            return None
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    # Take the leftmost forwarded values (client-facing) and drop the rest.
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",", 1)[0].strip()
    if not host or not _HOST_RE.match(host) or '"' in host or "\\" in host:
        return None
    proto = (headers.get("x-forwarded-proto") or "https").split(",", 1)[0].strip().lower() or "https"
    if proto not in ("http", "https"):
        return None
    # Canonical form must match the allowlist (case and default ports folded).
    # An empty allowlist cannot vouch for a client-supplied host.
    origin = normalize_origin(f"{proto}://{host}")
    if origin is None or origin not in allowed_origins():
        return None
    return origin


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


def protected_resource_document(headers: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Minimal RFC 9728 document, or ``None`` when the resource URI is unknown.

    ``resource`` is required to be an absolute URI. Callers should answer 404
    rather than publish a document that omits it.
    """
    headers = headers or {}
    resource = public_mcp_resource(headers)
    if not resource:
        return None
    doc_url = resource[: -len("/mcp")] + "/" if resource.endswith("/mcp") else resource
    return {
        "resource": resource,
        "bearer_methods_supported": ["header"],
        "resource_documentation": doc_url,
    }
