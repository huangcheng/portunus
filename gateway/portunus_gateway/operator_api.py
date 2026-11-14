"""Operator REST API: servers and tools (spec §8 dashboard back end).

Authentication lives in :mod:`operator_auth`. ``require_operator`` (session cookie + CSRF on
mutations) fails closed when no :class:`OperatorAuth` is attached to the app. Tests may
replace it through ``app.dependency_overrides``. Yields the operator identity for the audit log.
"""

from __future__ import annotations

import html
import logging
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
from .catalog import CatalogStore
from .oauth import OAuthError, OAuthStore, exchange_code, iss_matches, register_oauth_client
from .registry import Registry, RegistryError

logger = logging.getLogger("portunus.oauth")

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


def get_catalog(request: Request) -> CatalogStore:
    return request.app.state.catalog


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
    env: dict[str, str] | None = None


class EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class BearerTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=8192)


class OAuthStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str = Field(min_length=1, max_length=2048)


class CreateCatalogApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    connection: Literal["oauth", "http", "webdav", "stdio", "skill_bridge"]
    spec: dict[str, Any] = Field(default_factory=dict)
    popular: bool = False
    categories: list[str] = Field(default_factory=list, max_length=16)
    tags: list[str] = Field(default_factory=list, max_length=16)
    sort_order: int = Field(default=100, ge=0, le=10_000)


class UpdateCatalogApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    popular: bool | None = None
    categories: list[str] | None = Field(default=None, max_length=16)
    tags: list[str] | None = Field(default=None, max_length=16)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    spec: dict[str, Any] | None = None


router = APIRouter(prefix="/api")


@router.get("/apps")
async def list_catalog_apps(
    actor: str = Depends(require_operator),
    catalog: CatalogStore = Depends(get_catalog),
) -> list[dict[str, Any]]:
    return await catalog.list_apps()


def _toml_catalog_mutations_gone() -> None:
    from .toml_catalog import get_loaded
    from .registry import RegistryError

    if get_loaded() is not None:
        raise RegistryError(410, "gone", "catalog mutations are file-backed; edit catalog/apps/*.toml")


@router.post("/apps", status_code=201)
async def create_catalog_app(
    body: CreateCatalogApp,
    actor: str = Depends(require_operator),
    catalog: CatalogStore = Depends(get_catalog),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    _toml_catalog_mutations_gone()
    view = await catalog.create(
        slug=body.slug,
        label=body.label,
        name=body.name,
        description=body.description,
        connection=body.connection,
        spec=body.spec,
        popular=body.popular,
        categories=body.categories,
        tags=body.tags,
        sort_order=body.sort_order,
        actor=actor,
    )
    await _audit(registry, actor, "catalog.create", view["slug"], {"connection": body.connection})
    return view


@router.patch("/apps/{app_id}")
async def update_catalog_app(
    app_id: uuid.UUID,
    body: UpdateCatalogApp,
    actor: str = Depends(require_operator),
    catalog: CatalogStore = Depends(get_catalog),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    _toml_catalog_mutations_gone()
    view = await catalog.update(
        app_id,
        actor=actor,
        label=body.label,
        name=body.name,
        description=body.description,
        popular=body.popular,
        categories=body.categories,
        tags=body.tags,
        sort_order=body.sort_order,
        spec=body.spec,
    )
    await _audit(registry, actor, "catalog.update", view["slug"], {})
    return view


@router.delete("/apps/{app_id}", status_code=204)
async def delete_catalog_app(
    app_id: uuid.UUID,
    actor: str = Depends(require_operator),
    catalog: CatalogStore = Depends(get_catalog),
    registry: Registry = Depends(get_registry),
) -> None:
    _toml_catalog_mutations_gone()
    view = await catalog.get(app_id)
    await catalog.delete(app_id, actor=actor)
    await _audit(registry, actor, "catalog.delete", view["slug"], {})


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
        env=body.env,
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


class SkillAuthBody(BaseModel):
    action: str = "login"
    # oauth_link: pasted authorization code from the vendor authorize page.
    token: str | None = None
    # oauth_link: background login job id returned by the initial login call.
    job_id: str | None = None


@router.post("/servers/{server_id}/skill-auth")
async def skill_auth_server(
    server_id: uuid.UUID,
    body: SkillAuthBody,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Platform device_qr / oauth_link login for skill-bridge apps (Connect)."""
    return await registry.skill_auth(
        server_id, body.action, actor, token=body.token, job_id=body.job_id
    )


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
    """Begin OAuth code+PKCE; returns the authorize URL the operator should open.

    If the server has no ``client_id`` but a ``registration_endpoint`` (e.g. Ardot),
    register a public client first and persist the issued id.
    """
    cfg = await registry.remote_oauth_config(server_id)
    redirect_uri = body.redirect_uri.strip()
    client_id = cfg.get("client_id")
    if not client_id:
        reg = cfg.get("registration_endpoint")
        if not isinstance(reg, str) or not reg:
            raise RegistryError(422, "invalid_oauth", "oauth client_id is not configured")
        registered = await register_oauth_client(
            registration_endpoint=reg,
            redirect_uri=redirect_uri,
            scopes=list(cfg.get("scopes") or []),
            token_endpoint_auth_method=str(cfg.get("token_endpoint_auth_method") or "none"),
        )
        client_id = registered["client_id"]
        await registry.update_server(
            server_id,
            actor=actor,
            oauth={**cfg, "client_id": client_id},
        )
    store: OAuthStore = request.app.state.oauth
    started = store.start(
        server_id=server_id,
        authorize_url=cfg["authorize_url"],
        token_url=cfg["token_url"],
        client_id=client_id,
        scopes=list(cfg.get("scopes") or []),
        redirect_uri=redirect_uri,
        resource=cfg.get("resource"),
        issuer=cfg.get("issuer"),
    )
    await _audit(registry, actor, "server.oauth_start", str(server_id), {})
    return started


def _is_prefetch(request: Request) -> bool:
    """Browsers/CDNs sometimes prefetch the redirect URL; never exchange codes for those."""
    purpose = (request.headers.get("purpose") or request.headers.get("sec-purpose") or "").lower()
    return "prefetch" in purpose or "prerender" in purpose


def _oauth_fail_html(message: str, *, status: int) -> HTMLResponse:
    # Keep the window open so the operator can read the upstream error.
    safe = html.escape(message or "oauth failed")
    return HTMLResponse(
        "<html><body style='font:14px/1.45 system-ui;padding:24px;max-width:40rem'>"
        "<p><strong>OAuth failed</strong></p>"
        f"<p>{safe}</p>"
        "<p style='color:#666'>You can close this window and retry Authorize.</p>"
        "</body></html>",
        status_code=status if status < 600 else 502,
    )


async def _oauth_callback_result(
    request: Request,
    *,
    code: str,
    state: str,
    error: str,
    iss: str = "",
) -> Response:
    """Shared GET/POST callback handler (query + form_post response modes)."""
    if _is_prefetch(request):
        return Response(status_code=204)

    registry: Registry = request.app.state.registry
    store: OAuthStore = request.app.state.oauth
    try:
        # Peek first so a duplicate/racy hit does not drop PKCE before exchange runs.
        pending = store.get(state)
    except OAuthError:
        pending = None
    # RFC 9207 §2.4: validate the response issuer before trusting anything else in it —
    # on mismatch the code must not be exchanged and the error params must not be shown.
    if pending is not None and not iss_matches(iss, pending):
        logger.warning(
            "oauth callback iss mismatch server=%s expected=%r got=%r",
            pending.server_id,
            pending.issuer or pending.issuer_origin,
            iss[:256],
        )
        store.discard(state)
        return _oauth_fail_html("authorization response issuer (iss) mismatch", status=400)
    if error:
        store.discard(state)
        return _oauth_fail_html(f"authorization server: {error}", status=400)
    if pending is None:
        # Public unauthenticated endpoint: this warning is the only probe/replay signal.
        logger.warning("oauth callback with unknown or expired state")
        return _oauth_fail_html("unknown or expired oauth state", status=400)
    try:
        cfg = await registry.remote_oauth_config(pending.server_id)
        # Never send client_secret Basic auth for public clients (Ardot: auth method none).
        method = cfg.get("token_endpoint_auth_method") or "none"
        secret = None
        if method != "none":
            secret = await registry.read_oauth_client_secret(pending.server_id)
        authorization = await exchange_code(pending, code, client_secret=secret)
        store.discard(state)
        await registry.store_authorization_header(
            pending.server_id, authorization, actor="oauth-callback"
        )
    except (RegistryError, OAuthError) as e:
        logger.warning(
            "oauth callback failed code=%s status=%s msg=%s",
            getattr(e, "code", type(e).__name__),
            getattr(e, "status", "?"),
            getattr(e, "message", str(e)),
        )
        # Drop state after a real attempt so the one-time code cannot be replayed forever.
        store.discard(state)
        return _oauth_fail_html(e.message or e.code, status=e.status)
    return HTMLResponse(
        "<html><body style='font:14px/1.45 system-ui;padding:24px'>"
        "<p>Connected. You can close this window.</p>"
        "<script>window.opener&&window.opener.postMessage({portunusOauth:'ok'},'*');"
        "window.close()</script></body></html>"
    )


@router.get("/oauth/callback")
async def oauth_callback_get(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    iss: str = "",
) -> Response:
    """Browser redirect target after the upstream IdP (``response_mode=query``)."""
    return await _oauth_callback_result(request, code=code, state=state, error=error, iss=iss)


@router.post("/oauth/callback")
async def oauth_callback_post(request: Request) -> Response:
    """``response_mode=form_post`` variant used by some authorization servers."""
    form = await request.form()
    return await _oauth_callback_result(
        request,
        code=str(form.get("code") or ""),
        state=str(form.get("state") or ""),
        error=str(form.get("error") or ""),
        iss=str(form.get("iss") or ""),
    )


@router.options("/oauth/callback")
async def oauth_callback_options() -> Response:
    """Some browsers preflight the redirect URL; allow GET/POST without auth."""
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
    )


@router.post("/servers/{server_id}/enable")
async def enable_server(
    server_id: uuid.UUID,
    body: EnableBody | None = None,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.set_server_enabled(server_id, (body or EnableBody()).enabled, actor)


@router.post("/tools/{tool_id}/enable")
async def enable_tool(
    tool_id: uuid.UUID,
    body: EnableBody | None = None,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.set_tool_enabled(tool_id, (body or EnableBody()).enabled, actor)


class SchemaOverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``schema`` is the JSON key; attribute cannot be named ``schema`` (shadows BaseModel).
    schema_body: dict[str, Any] = Field(alias="schema")


@router.put("/tools/{tool_id}/schema-override")
async def set_tool_schema_override(
    tool_id: uuid.UUID,
    body: SchemaOverrideBody,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.set_tool_schema_override(tool_id, body.schema_body, actor)


@router.delete("/tools/{tool_id}/schema-override")
async def clear_tool_schema_override(
    tool_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    return await registry.clear_tool_schema_override(tool_id, actor)


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


@router.get("/status")
async def operator_status(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    """Authenticated fleet status (public ``/healthz`` omits runner counts — review #8)."""
    pool = getattr(request.app.state, "runner_pool", None)
    connected = list(pool.connected()) if pool is not None else []
    return {
        "ok": True,
        "runner_connected": bool(connected),
        "runners_connected": len(connected),
    }


@router.get("/fleet")
async def operator_fleet(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    from .db.session import async_session
    from .db.url import database_url
    from .fleet import build_fleet

    pool = getattr(request.app.state, "runner_pool", None)
    registry = getattr(request.app.state, "registry", None)
    slots = getattr(registry, "slots", None) if registry is not None else None
    if pool is None:
        raise RegistryError(503, "unsupported", "runner pool is not configured")
    started = getattr(request.app.state, "started_at", None)
    if not isinstance(started, datetime):
        started = datetime.now(timezone.utc)
    slug_map: dict[uuid.UUID, str] = {}
    async with async_session() as s:
        for sid, slug in (await s.execute(select(Server.id, Server.slug))).all():
            slug_map[sid] = slug
    site_health: list[dict[str, Any]] = []
    if registry is not None and hasattr(registry, "list_site_health"):
        site_health = await registry.list_site_health()
    return build_fleet(
        pool=pool,
        slots=slots,
        started_at=started,
        database_url=database_url(),
        server_slug=slug_map,
        site_health=site_health,
    )


@router.get("/audit")
async def list_audit(
    request: Request,
    actor: str = Depends(require_operator),
    limit: int = 50,
    before: str | None = None,
    action: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    from .db.session import async_session

    limit = max(1, min(int(limit), 200))
    async with async_session() as s:
        stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        if action:
            stmt = stmt.where(AuditEvent.action.startswith(action))
        if target:
            stmt = stmt.where(AuditEvent.target == target)
        if before:
            try:
                ts_s, id_s = before.split("|", 1)
                ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                bid = uuid.UUID(id_s)
            except (ValueError, TypeError) as e:
                raise RegistryError(422, "invalid_cursor", "bad before cursor") from e
            stmt = stmt.where(
                (AuditEvent.created_at < ts)
                | ((AuditEvent.created_at == ts) & (AuditEvent.id < bid))
            )
        rows = (await s.execute(stmt.limit(limit + 1))).scalars().all()
    page = rows[:limit]
    nxt = None
    if len(rows) > limit:
        last = page[-1]
        ts = last.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        nxt = f"{ts.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}|{last.id}"
    return {
        "items": [
            {
                "id": str(r.id),
                "created_at": _iso(r.created_at),
                "actor": r.actor,
                "action": r.action,
                "target": r.target,
                "detail": r.detail,
            }
            for r in page
        ],
        "next": nxt,
    }


@router.get("/tool-calls")
async def list_tool_calls(
    request: Request,
    actor: str = Depends(require_operator),
    limit: int = 50,
    before: str | None = None,
    server: str | None = None,
    outcome: str | None = None,
    key_id: str | None = None,
    site_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    from .db.models import ToolCall
    from .db.session import async_session

    limit = max(1, min(int(limit), 200))
    async with async_session() as s:
        stmt = select(ToolCall).order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
        if server:
            stmt = stmt.where(ToolCall.server_slug == server.casefold())
        if outcome:
            stmt = stmt.where(ToolCall.outcome == outcome)
        if key_id:
            stmt = stmt.where(ToolCall.key_id == key_id)
        if site_id:
            stmt = stmt.where(ToolCall.site_id == site_id)
        if trace_id:
            stmt = stmt.where(ToolCall.trace_id == trace_id)
        if before:
            try:
                ts_s, id_s = before.split("|", 1)
                ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                bid = uuid.UUID(id_s)
            except (ValueError, TypeError) as e:
                raise RegistryError(422, "invalid_cursor", "bad before cursor") from e
            stmt = stmt.where(
                (ToolCall.created_at < ts) | ((ToolCall.created_at == ts) & (ToolCall.id < bid))
            )
        rows = (await s.execute(stmt.limit(limit + 1))).scalars().all()
    page = rows[:limit]
    nxt = None
    if len(rows) > limit:
        last = page[-1]
        ts = last.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        nxt = f"{ts.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}|{last.id}"
    return {
        "items": [
            {
                "id": str(r.id),
                "created_at": _iso(r.created_at),
                "trace_id": r.trace_id,
                "key_id": r.key_id,
                "server_slug": r.server_slug,
                "tool": r.tool,
                "outcome": r.outcome,
                "site_id": r.site_id,
                "cold_start": r.cold_start,
                "preferred_miss": r.preferred_miss,
                "gateway_ms": r.gateway_ms,
                "start_ms": r.start_ms,
                "queue_ms": r.queue_ms,
                "init_ms": r.init_ms,
                "upstream_ms": r.upstream_ms,
                "result_bytes": r.result_bytes,
            }
            for r in page
        ],
        "next": nxt,
    }


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
    app.state.catalog = CatalogStore(registry.session_factory)
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
