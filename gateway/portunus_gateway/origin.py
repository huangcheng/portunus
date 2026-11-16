"""Streamable HTTP Origin allowlist (MCP 2026-07-28 DNS-rebinding MUST).

Absent ``Origin`` is allowed (non-browser agents). A present Origin must match
``PORTUNUS_ALLOWED_ORIGINS`` (comma-separated), or the origin of
``PORTUNUS_PUBLIC_URL``, plus localhost origins when ``PORTUNUS_DEV=1``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlparse


def _origin_of(url: str) -> str | None:
    try:
        parsed = urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return None
        # Browsers serialize Origin with a lowercase scheme/host; drop default ports.
        # hostname/port raise ValueError on unbalanced IPv6 literals and bad ports.
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def normalize_origin(url: str) -> str | None:
    """Lowercase scheme/host with default ports omitted. None when ``url`` is not an origin."""
    return _origin_of(url)


@lru_cache(maxsize=1)
def allowed_origins() -> frozenset[str]:
    """Return the configured Origin allowlist (call ``clear_origin_cache`` in tests)."""
    raw = os.environ.get("PORTUNUS_ALLOWED_ORIGINS", "").strip()
    out: set[str] = set()
    if raw:
        for part in raw.split(","):
            item = part.strip()
            if not item:
                continue
            # Accept bare origins or full URLs. Bare hosts need a scheme to match
            # real Origin headers; assume https unless the entry looks like localhost.
            if "://" in item:
                origin = _origin_of(item)
            else:
                host = item.rstrip("/")
                scheme = "http" if host.startswith(("localhost", "127.0.0.1", "[::1]")) else "https"
                origin = _origin_of(f"{scheme}://{host}")
            if origin:
                out.add(origin)
    public = os.environ.get("PORTUNUS_PUBLIC_URL", "").strip()
    if public:
        origin = _origin_of(public)
        if origin:
            out.add(origin)
    if os.environ.get("PORTUNUS_DEV") == "1":
        out.update(
            {
                "http://127.0.0.1:8080",
                "http://localhost:8080",
                "http://127.0.0.1:3000",
                "http://localhost:3000",
            }
        )
    return frozenset(out)


def clear_origin_cache() -> None:
    allowed_origins.cache_clear()


def origin_allowed(origin: str | None) -> bool:
    """True when the request may proceed.

    Missing Origin → allow. Present Origin → must be in the allowlist.
    """
    if origin is None or origin == "":
        return True
    normalized = _origin_of(origin)
    return normalized is not None and normalized in allowed_origins()
