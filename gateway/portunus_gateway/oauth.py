"""Operator-driven OAuth 2.1 authorization-code + PKCE for remote MCP servers (spec §6.3).

Paste-token is handled by the registry (vault ``Authorization`` header). This module only
covers the browser redirect: start → authorize URL, callback → token exchange → vault.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

OAUTH_STATE_TTL_S = 600
OAUTH_CLIENT_SECRET_NAME = "portunus.oauth_client_secret"
AUTH_HEADER_NAME = "Authorization"


class OAuthError(Exception):
    """Operator-facing OAuth failure; mapped to ``RegistryError`` at the API boundary."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class OAuthPending:
    server_id: uuid.UUID
    code_verifier: str
    token_url: str
    client_id: str
    redirect_uri: str
    created_at: float


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def validate_oauth_config(raw: Any) -> dict[str, Any] | None:
    """Normalize optional oauth config. ``None`` / empty → no OAuth."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise OAuthError(422, "invalid_oauth", "oauth must be an object")
    authorize_url = raw.get("authorize_url")
    token_url = raw.get("token_url")
    client_id = raw.get("client_id")
    scopes = raw.get("scopes", [])
    if not all(isinstance(x, str) and x.strip() for x in (authorize_url, token_url, client_id)):
        raise OAuthError(422, "invalid_oauth", "authorize_url, token_url, client_id required")
    for label, url in (("authorize_url", authorize_url), ("token_url", token_url)):
        parsed = urlparse(url.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise OAuthError(422, "invalid_oauth", f"{label} must be https")
    if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
        raise OAuthError(422, "invalid_oauth", "scopes must be a list of strings")
    if len(scopes) > 32 or sum(len(s) for s in scopes) > 2048:
        raise OAuthError(422, "invalid_oauth", "scopes too large")
    out: dict[str, Any] = {
        "authorize_url": authorize_url.strip(),
        "token_url": token_url.strip(),
        "client_id": client_id.strip(),
        "scopes": list(scopes),
    }
    return out


def oauth_public_view(oauth: dict[str, Any] | None) -> dict[str, Any] | None:
    if not oauth:
        return None
    return {
        "authorize_url": oauth.get("authorize_url"),
        "token_url": oauth.get("token_url"),
        "client_id": oauth.get("client_id"),
        "scopes": list(oauth.get("scopes") or []),
        "configured": True,
    }


class OAuthStore:
    """In-memory PKCE state (single gateway worker)."""

    def __init__(self, *, ttl_s: float = OAUTH_STATE_TTL_S) -> None:
        self._ttl = ttl_s
        self._pending: dict[str, OAuthPending] = {}

    def _purge(self) -> None:
        now = time.monotonic()
        dead = [k for k, v in self._pending.items() if now - v.created_at > self._ttl]
        for k in dead:
            self._pending.pop(k, None)

    def start(
        self,
        *,
        server_id: uuid.UUID,
        authorize_url: str,
        token_url: str,
        client_id: str,
        scopes: list[str],
        redirect_uri: str,
    ) -> dict[str, str]:
        self._purge()
        verifier, challenge = new_pkce()
        state = _b64url(secrets.token_bytes(24))
        self._pending[state] = OAuthPending(
            server_id=server_id,
            code_verifier=verifier,
            token_url=token_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
            created_at=time.monotonic(),
        )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        sep = "&" if "?" in authorize_url else "?"
        return {"state": state, "authorize_url": f"{authorize_url}{sep}{urlencode(params)}"}

    def pop(self, state: str) -> OAuthPending:
        self._purge()
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthError(400, "invalid_oauth", "unknown or expired oauth state")
        return pending


async def exchange_code(
    pending: OAuthPending,
    code: str,
    *,
    client_secret: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Exchange ``code`` for an access token; returns a ready ``Authorization`` header value."""
    if not isinstance(code, str) or not code or "\x00" in code:
        raise OAuthError(400, "invalid_oauth", "missing authorization code")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": pending.client_id,
        "code_verifier": pending.code_verifier,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    auth: httpx.Auth | None = None
    if client_secret:
        auth = httpx.BasicAuth(pending.client_id, client_secret)
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=httpx.Timeout(30.0)
        ) as http:
            resp = await http.post(pending.token_url, data=data, headers=headers, auth=auth)
    except httpx.HTTPError as e:
        raise OAuthError(502, "upstream_crash", f"token endpoint failed: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise OAuthError(502, "auth_required", "token endpoint rejected the code")
    try:
        body = resp.json()
    except ValueError:
        raise OAuthError(502, "upstream_crash", "token endpoint returned non-JSON") from None
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise OAuthError(502, "upstream_crash", "token endpoint omitted access_token")
    token_type = str(body.get("token_type") or "Bearer").strip() or "Bearer"
    return f"{token_type} {token}"
