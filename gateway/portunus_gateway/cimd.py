"""OAuth Client ID Metadata Document (CIMD) for MCP authorization.

MCP 2026-07-28 prefers CIMD over Dynamic Client Registration when the AS
advertises ``client_id_metadata_document_supported``. The ``client_id`` is the
HTTPS URL of this document; the document's ``client_id`` field MUST match that
URL exactly.
"""

from __future__ import annotations

import os
from typing import Any

from .origin import normalize_origin


CIMD_PATH = "/oauth/client-metadata.json"


def public_origin() -> str | None:
    """Return ``PORTUNUS_PUBLIC_URL`` origin (no trailing slash), or None."""
    raw = os.environ.get("PORTUNUS_PUBLIC_URL", "").strip()
    if not raw:
        return None
    origin = normalize_origin(raw)
    if origin is None or not origin.startswith("https://"):
        return None
    return origin


def cimd_client_id() -> str | None:
    """Absolute CIMD URL used as OAuth ``client_id``, or None when not configured."""
    origin = public_origin()
    return f"{origin}{CIMD_PATH}" if origin else None


def cimd_document() -> dict[str, Any] | None:
    """Build the CIMD JSON body. Returns None when ``PORTUNUS_PUBLIC_URL`` is unset."""
    client_id = cimd_client_id()
    origin = public_origin()
    if not client_id or not origin:
        return None
    return {
        "client_id": client_id,
        "client_name": "Portunus",
        "client_uri": origin,
        "redirect_uris": [f"{origin}/api/oauth/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }
