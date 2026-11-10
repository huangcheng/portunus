"""Operator REST API: servers and tools (spec §8 dashboard back end).

Authentication lives in :mod:`operator_auth`. ``require_operator`` (session cookie + CSRF on
mutations) fails closed when no :class:`OperatorAuth` is attached to the app. Tests may
replace it through ``app.dependency_overrides``. Yields the operator identity for the audit log.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from . import authz, keys
from .db.models import AgentKey, AuditEvent, Grant, Server
from .operator_auth import (
    OPERATOR,
    SESSION_COOKIE,
    CsrfError,
    OperatorAuth,
)
from .oauth import OAuthError, OAuthStore, exchange_code
from .registry import Registry, RegistryError

SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$"


def _auth(request: Request) -> OperatorAuth:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        raise RegistryError(401, "unauthorized", "operator authentication is not configured")
    return auth


async def require_operator(request: Request) -> str:
    """Valid session cookie (+ CSRF header on non-safe methods), else 401/403."""
    auth = _auth(request)
    try:
        session = await auth.authenticate(request)
    except CsrfError:
        raise RegistryError(403, "csrf", "missing or invalid CSRF token") from None
    if session is None:
        raise RegistryError(401, "unauthorized", "login required")
    request.state.operator_session_id = session.id
    return OPERATOR



def get_registry(request: Request) -> Registry:
    return request.app.state.registry


class CreateServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: Literal["stdio", "remote"]
    command: str | None = Field(default=None, max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=64)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2048)
    preferred_regions: list[str] = Field(default_factory=list)
    oauth: dict[str, Any] | None = None
    oauth_client_secret: str | None = Field(default=None, max_length=4096)


class UpdateServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_regions: list[str] | None = None
    oauth: dict[str, Any] | None = None
    oauth_client_secret: str | None = Field(default=None, max_length=4096)


class EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class BearerTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=8192)


class OAuthStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str = Field(min_length=1, max_length=2048)


router = APIRouter(prefix="/api")


@router.post("/servers", status_code=201)
async def create_server(
    body: CreateServer,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    view = await registry.create_server(
        name=body.name,
        slug=body.slug,
        kind=body.kind,
        command=body.command,
        args=body.args,
        env=body.env,
        actor=actor,
        preferred_regions=body.preferred_regions,
        url=body.url,
        oauth=body.oauth,
        oauth_client_secret=body.oauth_client_secret,
    )
    await authz.ensure_full_grants()
    return view


@router.patch("/servers/{server_id}")
async def update_server(
    server_id: uuid.UUID,
    body: UpdateServer,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.update_server(
        server_id,
        actor=actor,
        preferred_regions=body.preferred_regions,
        oauth=body.oauth,
        oauth_client_secret=body.oauth_client_secret,
    )


@router.get("/servers")
async def list_servers(
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> list[dict[str, Any]]:
    return await registry.list_servers()


@router.get("/servers/{server_id}/tools")
async def list_tools(
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> list[dict[str, Any]]:
    return await registry.list_tools(server_id)


@router.post("/servers/{server_id}/probe")
async def probe_server(
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.probe(server_id, actor)


@router.post("/servers/{server_id}/auth/token")
async def set_server_token(
    server_id: uuid.UUID,
    body: BearerTokenBody,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Paste a bearer token (or full ``Authorization`` value) into the vault."""
    return await registry.set_bearer_token(server_id, body.token, actor)


@router.post("/servers/{server_id}/oauth/start")
async def oauth_start(
    request: Request,
    server_id: uuid.UUID,
    body: OAuthStartBody,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Begin OAuth code+PKCE; returns the authorize URL the operator should open."""
    cfg = await registry.remote_oauth_config(server_id)
    store: OAuthStore = request.app.state.oauth
    started = store.start(
        server_id=server_id,
        authorize_url=cfg["authorize_url"],
        token_url=cfg["token_url"],
        client_id=cfg["client_id"],
        scopes=list(cfg.get("scopes") or []),
        redirect_uri=body.redirect_uri.strip(),
    )
    await _audit(registry, actor, "server.oauth_start", str(server_id), {})
    return started


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> Response:
    """Browser redirect target after the upstream IdP; stores the token then closes the window."""
    registry: Registry = request.app.state.registry
    store: OAuthStore = request.app.state.oauth
    if error:
        return HTMLResponse(
            f"<html><body><p>OAuth error: {error}</p><script>window.close()</script></body></html>",
            status_code=400,
        )
    try:
        pending = store.pop(state)
        secret = await registry.read_oauth_client_secret(pending.server_id)
        authorization = await exchange_code(pending, code, client_secret=secret)
        await registry.store_authorization_header(
            pending.server_id, authorization, actor="oauth-callback"
        )
    except (RegistryError, OAuthError) as e:
        return HTMLResponse(
            f"<html><body><p>{e.message or e.code}</p><script>window.close()</script></body></html>",
            status_code=e.status,
        )
    return HTMLResponse(
        "<html><body><p>Connected. You can close this window.</p>"
        "<script>window.opener&&window.opener.postMessage({portunusOauth:'ok'},'*');"
        "window.close()</script></body></html>"
    )


@router.post("/servers/{server_id}/enable")
async def enable_server(
    server_id: uuid.UUID,
    body: EnableBody | None = None,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.set_server_enabled(server_id, (body or EnableBody()).enabled, actor)


# ---- auth routes ------------------------------------------------------------------------


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)


def _locked(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"code": "locked_out", "message": "too many failed attempts"},
        headers={"Retry-After": str(retry_after)},
    )


@router.post("/auth/login")
async def login(body: PasswordBody, request: Request, response: Response):
    auth = _auth(request)
    ip = auth.client_ip(request)
    ok, retry_after = await auth.check_password(ip, body.password)
    if retry_after:
        return _locked(retry_after)
    if not ok:
        raise RegistryError(401, "unauthorized", "invalid password")
    token, csrf, _sid = await auth.create_session()
    await auth.audit(OPERATOR, "operator.login", {"ip": ip})
    out = JSONResponse({"csrf_token": csrf})
    out.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=12 * 3600,
        httponly=True,
        secure=auth.secure_cookie,
        samesite="strict",
        path="/",
    )
    return out


@router.get("/auth/session")
async def session_info(request: Request, actor: str = Depends(require_operator)) -> dict[str, Any]:
    row = await _auth(request).lookup_session(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": True, "csrf_token": row.csrf_token if row else None}


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, actor: str = Depends(require_operator)) -> Response:
    auth = _auth(request)
    await auth.end_session(request.state.operator_session_id)
    await auth.audit(actor, "operator.logout")
    out = Response(status_code=204)
    out.delete_cookie(SESSION_COOKIE, path="/")
    return out


# ---- agent keys + grants ----------------------------------------------------------------


class MintKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _key_view(row: AgentKey, server_ids: list[uuid.UUID]) -> dict[str, Any]:
    return {
        "key_id": row.key_id,
        "label": row.label,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
        "has_secret": row.secret_ciphertext is not None and row.secret_key_version is not None,
        "server_ids": sorted(str(s) for s in server_ids),
    }


async def _audit(registry: Registry, actor: str, action: str, target: str, detail: dict) -> None:
    async with registry.session_factory() as s:
        s.add(AuditEvent(actor=actor, action=action, target=target, detail=detail))
        await s.commit()


async def _key_row_view(registry: Registry, key_id: str) -> dict[str, Any]:
    async with registry.session_factory() as s:
        row = (await s.execute(select(AgentKey).where(AgentKey.key_id == key_id))).scalar_one_or_none()
        if row is None:
            raise RegistryError(404, "not_found", "key not found")
        sids = list((await s.execute(select(Grant.server_id).where(Grant.key_id == key_id))).scalars())
    return _key_view(row, sids)


@router.post("/keys", status_code=201)
async def mint_key(
    body: MintKey,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    if body.expires_at is not None and _utc(body.expires_at) <= datetime.now(timezone.utc):
        raise RegistryError(422, "invalid_request", "expires_at must be in the future")
    token, row = await keys.mint_agent_key(
        expires_at=body.expires_at, label=body.label, vault=registry.vault
    )
    await authz.ensure_full_grants()
    await _audit(registry, actor, "key.mint", row.key_id, {"label": body.label})
    view = await _key_row_view(registry, row.key_id)
    return {**view, "token": token}


@router.get("/keys")
async def list_keys(
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> list[dict[str, Any]]:
    async with registry.session_factory() as s:
        rows = (
            await s.execute(
                select(AgentKey)
                .where(AgentKey.revoked_at.is_(None))
                .order_by(AgentKey.created_at)
            )
        ).scalars().all()
        grants: dict[str, list[uuid.UUID]] = {}
        for key_id, server_id in await s.execute(select(Grant.key_id, Grant.server_id)):
            grants.setdefault(key_id, []).append(server_id)
    return [_key_view(r, grants.get(r.key_id, [])) for r in rows]


@router.get("/keys/{key_id}/secret")
async def reveal_key_secret(
    key_id: str,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, str]:
    try:
        token = await keys.reveal_agent_token(key_id, registry.vault)
    except keys.AuthError:
        raise RegistryError(404, "not_found", "key not found") from None
    await _audit(registry, actor, "key.reveal", key_id, {})
    return {"token": token}


@router.post("/keys/{key_id}/revoke", status_code=204)
@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(
    key_id: str,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> None:
    try:
        await keys.revoke_key(key_id)
    except keys.AuthError:
        raise RegistryError(404, "not_found", "key not found") from None
    if registry.slots is not None:
        registry.slots.revoke_key(key_id)  # children -> pending_stop
    await _audit(registry, actor, "key.revoke", key_id, {})


@router.put("/keys/{key_id}/grants/{server_id}")
async def grant_server(
    key_id: str,
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    async with registry.session_factory() as s:
        key = (await s.execute(select(AgentKey).where(AgentKey.key_id == key_id))).scalar_one_or_none()
        if key is None or await s.get(Server, server_id) is None:
            raise RegistryError(404, "not_found", "key or server not found")
        exists = (
            await s.execute(select(Grant).where(Grant.key_id == key_id, Grant.server_id == server_id))
        ).first()
        if exists is None:
            s.add(Grant(key_id=key_id, server_id=server_id))
            await s.commit()
    await authz.authz_store.reload_from_db()
    await _audit(registry, actor, "grant.add", key_id, {"server_id": str(server_id)})
    return await _key_row_view(registry, key_id)


@router.delete("/keys/{key_id}/grants/{server_id}")
async def ungrant_server(
    key_id: str,
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    async with registry.session_factory() as s:
        await s.execute(delete(Grant).where(Grant.key_id == key_id, Grant.server_id == server_id))
        await s.commit()
    await authz.authz_store.reload_from_db()
    if registry.slots is not None:
        registry.slots.ungrant(key_id, server_id)  # pair -> pending_stop
    await _audit(registry, actor, "grant.remove", key_id, {"server_id": str(server_id)})
    return await _key_row_view(registry, key_id)


def create_app(registry: Registry, auth: OperatorAuth | None = None) -> FastAPI:
    app = FastAPI(title="Portunus gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    app.state.auth = auth
    app.state.oauth = OAuthStore()
    app.include_router(router)

    @app.exception_handler(RegistryError)
    async def _registry_error(_request: Request, exc: RegistryError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "message": exc.message}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths/types only: never echo submitted values (env literals are secrets).
        errors = [
            {"loc": [str(p) for p in e["loc"]], "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content={"code": "invalid_request", "errors": errors}
        )

    return app
