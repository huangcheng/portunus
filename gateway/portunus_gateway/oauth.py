"""Operator-driven OAuth 2.1 authorization-code + PKCE for remote MCP servers (spec §6.3).

Paste-token is handled by the registry (vault ``Authorization`` header). This module covers
the browser redirect (start → authorize URL, callback → token exchange → vault) and the
``refresh_token`` grant used when a short-lived access token expires.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

OAUTH_STATE_TTL_S = 600
OAUTH_CLIENT_SECRET_NAME = "portunus.oauth_client_secret"
OAUTH_REFRESH_TOKEN_NAME = "portunus.oauth_refresh_token"
AUTH_HEADER_NAME = "Authorization"


@dataclass(frozen=True)
class TokenBundle:
    """Access token (as Authorization header value) plus optional refresh material."""

    authorization: str
    refresh_token: str | None = None
    expires_in: int | None = None


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
    resource: str | None = None
    # RFC 9207 mix-up defense: expected issuer (exact match when configured explicitly)
    # and the authorize endpoint's origin (anchor when no explicit issuer is known).
    issuer: str | None = None
    issuer_origin: str | None = None


def _origin_of(url: str) -> str | None:
    """Normalized origin: lowercase scheme/host, explicit default port dropped (RFC 3986 §6.2.3)."""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    if default_port is not None and netloc.endswith(f":{default_port}"):
        netloc = netloc[: -len(f":{default_port}")]
    return f"{scheme}://{netloc}"


def iss_matches(iss: str | None, pending: OAuthPending) -> bool:
    """Validate the RFC 9207 ``iss`` parameter of an authorization response.

    Exact issuer comparison needs the AS's issuer identifier from metadata discovery,
    which this operator-configured flow deliberately does not perform. With an explicit
    ``issuer`` configured, RFC 9207 requires ``iss`` and we compare it exactly
    (RFC 3986 §6.2.1, no normalization). Otherwise we anchor on the authorize endpoint's
    origin — a response from a different authority is rejected while tenant paths are
    tolerated. An absent ``iss`` is accepted only in that fallback: without a configured
    issuer, only AS metadata says whether the AS sends ``iss``.
    """
    # When an exact issuer is configured, RFC 9207 mix-up defense requires ``iss``.
    # Omitting it must not skip the check. Without a configured issuer we still
    # accept a missing ``iss`` (AS metadata alone says whether the AS sends it).
    if pending.issuer:
        return bool(iss) and iss == pending.issuer
    if not iss:
        return True
    got = _origin_of(iss)
    return got is not None and got == pending.issuer_origin


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _https_url(label: str, url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        raise OAuthError(422, "invalid_oauth", f"{label} required")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise OAuthError(422, "invalid_oauth", f"{label} must be https")
    return url.strip()


def validate_oauth_config(raw: Any) -> dict[str, Any] | None:
    """Normalize optional oauth config. ``None`` / empty → no OAuth.

    ``client_id`` may be omitted when ``registration_endpoint`` is set (DCR on first authorize).
    ``resource`` (RFC 8707) is optional but required by peers like Ardot.
    """
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise OAuthError(422, "invalid_oauth", "oauth must be an object")
    authorize_url = _https_url("authorize_url", raw.get("authorize_url"))
    token_url = _https_url("token_url", raw.get("token_url"))
    scopes = raw.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
        raise OAuthError(422, "invalid_oauth", "scopes must be a list of strings")
    if len(scopes) > 32 or sum(len(s) for s in scopes) > 2048:
        raise OAuthError(422, "invalid_oauth", "scopes too large")
    client_id = raw.get("client_id")
    registration_endpoint = raw.get("registration_endpoint")
    use_cimd = raw.get("use_cimd") is True
    if client_id is not None and client_id != "":
        if not isinstance(client_id, str) or not client_id.strip():
            raise OAuthError(422, "invalid_oauth", "client_id must be a non-empty string")
        client_id = client_id.strip()
    else:
        client_id = None
    if registration_endpoint:
        registration_endpoint = _https_url("registration_endpoint", registration_endpoint)
    if not client_id and not registration_endpoint and not use_cimd:
        raise OAuthError(
            422,
            "invalid_oauth",
            "client_id required (or registration_endpoint for DCR, or use_cimd)",
        )
    out: dict[str, Any] = {
        "authorize_url": authorize_url,
        "token_url": token_url,
        "scopes": list(scopes),
    }
    if client_id:
        out["client_id"] = client_id
    if registration_endpoint:
        out["registration_endpoint"] = registration_endpoint
    if use_cimd:
        out["use_cimd"] = True
    resource = raw.get("resource")
    if resource:
        out["resource"] = _https_url("resource", resource)
    issuer = raw.get("issuer")
    if issuer is not None and issuer != "":
        # Optional RFC 9207 issuer identifier; when set, callback ``iss`` must match exactly.
        if not isinstance(issuer, str):
            raise OAuthError(422, "invalid_oauth", "issuer must be a string")
        out["issuer"] = _https_url("issuer", issuer)
    method = raw.get("token_endpoint_auth_method")
    if method:
        if method not in ("none", "client_secret_basic", "client_secret_post"):
            raise OAuthError(422, "invalid_oauth", "invalid token_endpoint_auth_method")
        out["token_endpoint_auth_method"] = method
    return out


def oauth_public_view(oauth: dict[str, Any] | None) -> dict[str, Any] | None:
    if not oauth:
        return None
    return {
        "authorize_url": oauth.get("authorize_url"),
        "token_url": oauth.get("token_url"),
        "client_id": oauth.get("client_id") or "",
        "scopes": list(oauth.get("scopes") or []),
        "resource": oauth.get("resource"),
        "issuer": oauth.get("issuer"),
        "registration_endpoint": oauth.get("registration_endpoint"),
        "token_endpoint_auth_method": oauth.get("token_endpoint_auth_method"),
        "use_cimd": bool(oauth.get("use_cimd")),
        "configured": bool(oauth.get("authorize_url") and oauth.get("token_url")),
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
        resource: str | None = None,
        issuer: str | None = None,
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
            resource=resource,
            issuer=issuer,
            issuer_origin=_origin_of(authorize_url),
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
        if resource:
            params["resource"] = resource
        # Merge into the query component. Concatenation drops params into a fragment
        # (``https://as/authorize#/cb``) and turns a trailing ``?`` into ``?&``.
        parsed = urlparse(authorize_url)
        existing = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in params
        ]
        authorize = urlunparse(parsed._replace(query=urlencode([*existing, *params.items()])))
        return {"state": state, "authorize_url": authorize}

    def get(self, state: str) -> OAuthPending:
        self._purge()
        pending = self._pending.get(state)
        if pending is None:
            raise OAuthError(400, "invalid_oauth", "unknown or expired oauth state")
        return pending

    def pop(self, state: str) -> OAuthPending:
        self._purge()
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthError(400, "invalid_oauth", "unknown or expired oauth state")
        return pending

    def discard(self, state: str) -> None:
        self._pending.pop(state, None)


def _dcr_application_type(redirect_uri: str) -> str:
    """OIDC ``application_type`` derived from the redirect target (MCP 2026-07-28 client MUST).

    OIDC requires web clients to register https redirect URIs; plain-http redirects (dev
    dashboards, loopback) fit the native profile instead. An AS enforcing the rule would
    reject a hardcoded "web" over an http redirect with ``invalid_redirect_uri``.
    """
    return "web" if redirect_uri.strip().lower().startswith("https://") else "native"


async def register_oauth_client(
    *,
    registration_endpoint: str,
    redirect_uri: str,
    client_name: str = "Portunus",
    scopes: list[str] | None = None,
    token_endpoint_auth_method: str = "none",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """RFC 7591 dynamic client registration. Returns the registration response body."""
    body: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": token_endpoint_auth_method,
        # MCP 2026-07-28 client MUST: application_type must be explicit (OIDC defaults
        # an omitted value to "web", which then conflicts with non-https redirects).
        "application_type": _dcr_application_type(redirect_uri),
    }
    if scopes:
        body["scope"] = " ".join(scopes)
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=httpx.Timeout(30.0)
        ) as http:
            resp = await http.post(
                registration_endpoint,
                json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise OAuthError(
            502, "upstream_crash", f"registration endpoint failed: {type(e).__name__}"
        ) from e
    if resp.status_code >= 400:
        detail = "dynamic client registration rejected"
        try:
            err_body = resp.json()
        except ValueError:
            err_body = None
        if isinstance(err_body, dict):
            err = err_body.get("error")
            desc = err_body.get("error_description")
            if isinstance(err, str) and isinstance(desc, str) and desc.strip():
                detail = f"DCR rejected: {err} — {desc.strip()[:240]}"
            elif isinstance(err, str) and err.strip():
                detail = f"DCR rejected: {err}"
        raise OAuthError(502, "invalid_oauth", detail)
    try:
        data = resp.json()
    except ValueError:
        raise OAuthError(502, "upstream_crash", "registration endpoint returned non-JSON") from None
    if not isinstance(data, dict):
        raise OAuthError(502, "upstream_crash", "registration endpoint returned non-object JSON")
    client_id = data.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise OAuthError(502, "upstream_crash", "registration omitted client_id")
    return data


def _apply_token_client_auth(
    data: dict[str, str],
    *,
    client_id: str,
    client_secret: str | None,
    auth_method: str | None,
) -> httpx.Auth | None:
    """Attach a client secret the way ``token_endpoint_auth_method`` requires.

    ``client_secret_post`` (GitHub) puts the secret in the form body.
    ``client_secret_basic`` uses HTTP Basic. ``none`` sends nothing.
    A secret with no declared method keeps the historical Basic default.
    """
    if not client_secret:
        return None
    method = auth_method or "client_secret_basic"
    if method == "client_secret_post":
        data["client_secret"] = client_secret
        return None
    if method == "none":
        return None
    if method == "client_secret_basic":
        return httpx.BasicAuth(client_id, client_secret)
    return None


def _token_error_message(resp: httpx.Response) -> str:
    """Prefer upstream ``error`` / ``error_description`` over a generic rejection."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        desc = body.get("error_description") or body.get("hint")
        if isinstance(err, str) and isinstance(desc, str) and desc.strip():
            return f"token endpoint: {err} — {desc.strip()[:240]}"
        if isinstance(err, str) and err.strip():
            return f"token endpoint: {err}"
    return f"token endpoint rejected the code (HTTP {resp.status_code})"


def _has_header_ctl(value: str) -> bool:
    """True when ``value`` contains a byte that must not appear in an HTTP header."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _parse_token_response(body: Any) -> TokenBundle:
    if not isinstance(body, dict):
        raise OAuthError(502, "upstream_crash", "token endpoint returned non-object JSON")
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise OAuthError(502, "upstream_crash", "token endpoint omitted access_token")
    if _has_header_ctl(token):
        raise OAuthError(502, "upstream_crash", "token endpoint returned an invalid access_token")
    # RFC 6749 token types are visible ASCII (%x21-7E). The single SP before the
    # access token is added here; whitespace or controls inside token_type would
    # be header injection when this value is replayed as Authorization.
    token_type = str(body.get("token_type") or "Bearer").strip() or "Bearer"
    if not token_type.isascii() or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token_type):
        raise OAuthError(502, "upstream_crash", "token endpoint returned an invalid token_type")
    refresh = body.get("refresh_token")
    refresh_token = refresh if isinstance(refresh, str) and refresh.strip() else None
    expires_raw = body.get("expires_in")
    expires_in: int | None = None
    if isinstance(expires_raw, bool):
        expires_in = None
    elif isinstance(expires_raw, int) and expires_raw > 0:
        expires_in = expires_raw
    elif isinstance(expires_raw, float) and expires_raw > 0:
        expires_in = int(expires_raw)
    return TokenBundle(
        authorization=f"{token_type} {token}",
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


async def exchange_code(
    pending: OAuthPending,
    code: str,
    *,
    client_secret: str | None = None,
    token_endpoint_auth_method: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenBundle:
    """Exchange ``code`` for tokens; returns Authorization + optional refresh/expiry."""
    if not isinstance(code, str) or not code or "\x00" in code:
        raise OAuthError(400, "invalid_oauth", "missing authorization code")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": pending.client_id,
        "code_verifier": pending.code_verifier,
    }
    if pending.resource:
        # RFC 8707: required when the authorize request included ``resource`` (e.g. Ardot).
        data["resource"] = pending.resource
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    auth = _apply_token_client_auth(
        data,
        client_id=pending.client_id,
        client_secret=client_secret,
        auth_method=token_endpoint_auth_method,
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=httpx.Timeout(30.0)
        ) as http:
            resp = await http.post(pending.token_url, data=data, headers=headers, auth=auth)
    except httpx.HTTPError as e:
        raise OAuthError(502, "upstream_crash", f"token endpoint failed: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise OAuthError(502, "auth_required", _token_error_message(resp))
    try:
        body = resp.json()
    except ValueError:
        raise OAuthError(502, "upstream_crash", "token endpoint returned non-JSON") from None
    return _parse_token_response(body)


async def refresh_access_token(
    *,
    token_url: str,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
    token_endpoint_auth_method: str | None = None,
    resource: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenBundle:
    """OAuth ``refresh_token`` grant → new Authorization (+ rotated refresh when returned)."""
    if not isinstance(refresh_token, str) or not refresh_token.strip() or "\x00" in refresh_token:
        raise OAuthError(400, "invalid_oauth", "missing refresh token")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token.strip(),
        "client_id": client_id,
    }
    if resource:
        data["resource"] = resource
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    auth = _apply_token_client_auth(
        data,
        client_id=client_id,
        client_secret=client_secret,
        auth_method=token_endpoint_auth_method,
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=httpx.Timeout(30.0)
        ) as http:
            resp = await http.post(token_url, data=data, headers=headers, auth=auth)
    except httpx.HTTPError as e:
        raise OAuthError(502, "upstream_crash", f"token endpoint failed: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise OAuthError(502, "auth_required", _token_error_message(resp))
    try:
        body = resp.json()
    except ValueError:
        raise OAuthError(502, "upstream_crash", "token endpoint returned non-JSON") from None
    bundle = _parse_token_response(body)
    # Some IdPs omit refresh_token on refresh; keep the prior one.
    if bundle.refresh_token is None:
        return TokenBundle(
            authorization=bundle.authorization,
            refresh_token=refresh_token.strip(),
            expires_in=bundle.expires_in,
        )
    return bundle
