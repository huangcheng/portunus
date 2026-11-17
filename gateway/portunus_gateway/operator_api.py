"""Operator REST API: servers and tools (spec §8 dashboard back end).

Authentication lives in :mod:`operator_auth`. ``require_operator`` (session cookie + CSRF on
mutations) fails closed when no :class:`OperatorAuth` is attached to the app. Tests may
replace it through ``app.dependency_overrides``. Yields the operator identity for the audit log.
"""

from __future__ import annotations

import html
import hmac
import logging
import os
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from . import authz, keys
from . import passkey as pk
from .db.models import AgentKey, AuditEvent, Grant, Server
from .operator_account import (
    consume_recovery_code,
    confirm_totp,
    RECOVERY_COUNT,
    count_passkeys,
    create_account,
    delete_passkey,
    disable_totp,
    fresh_totp_secret,
    generate_recovery_codes,
    get_account,
    get_passkey,
    list_passkeys,
    lockout_retry_after,
    normalize_recovery_input,
    open_totp_secret,
    passkey_allows,
    record_auth_event,
    recovery_counts,
    replace_recovery_codes,
    save_passkey,
    set_password_hash,
    set_totp_step,
    totp_provisioning_uri,
    totp_verify_at,
    touch_passkey,
)
from .operator_auth import (
    OPERATOR,
    PASSWORD_ENV,
    SESSION_COOKIE,
    STEP_UP_HEADER,
    CsrfError,
    OperatorAuth,
)
from .origin import allowed_origins
from .operator_crypto import OperatorCryptoError
from .turnstile import token_looks_plausible, turnstile_secret, turnstile_sitekey, verify_login_token
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


async def require_step_up(request: Request, actor: str = Depends(require_operator)) -> str:
    """Operator session plus a single-use ``X-Step-Up-Token``.

    Reserved for account security surfaces (2FA, recovery keys, passkeys).
    Key mint/reveal and Connect use the operator session alone.
    """
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        return actor
    sid = getattr(request.state, "operator_session_id", None)
    if sid is None or not auth.verify_step_up(sid, request.headers.get(STEP_UP_HEADER)):
        raise RegistryError(401, "step_up_required", "re-authenticate with a step-up token")
    return actor


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
    # Catalog Connect only: must equal slug; freezes identity to the recipe.
    recipe_slug: str | None = Field(default=None, pattern=SLUG_PATTERN)


class UpdateServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Custom servers only (recipe_slug IS NULL); catalog Connect freezes slug = recipe.
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    # Stdio argv — custom servers only; revalidated against the closed grammar.
    command: str | None = Field(default=None, max_length=1024)
    args: list[str] | None = Field(default=None, max_length=64)
    preferred_regions: list[str] | None = None
    oauth: dict[str, Any] | None = None
    oauth_client_secret: str | None = Field(default=None, max_length=4096)
    env: dict[str, str] | None = None
    # Remote MCP endpoint URL (e.g. self-hosted WeKnora /mcp/<endpoint_id>).
    url: str | None = Field(default=None, max_length=2048)


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
    request: Request,
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
    request: Request,
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
        recipe_slug=body.recipe_slug,
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
        name=body.name,
        slug=body.slug,
        command=body.command,
        args=body.args,
        preferred_regions=body.preferred_regions,
        oauth=body.oauth,
        oauth_client_secret=body.oauth_client_secret,
        env=body.env,
        url=body.url,
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
    """Run a probe and ensure the durable ServerProbeWorkflow is running when Temporal is up.

    The HTTP response stays a synchronous registry probe (dashboard needs tools/status).
    Background / post-credential probes go through Temporal only (see Registry._schedule_probe).
    """
    view = await registry.probe(server_id, actor)
    try:
        from portunus_worker.client import start_server_probe, temporal_enabled

        if temporal_enabled():
            await start_server_probe(server_id)
    except Exception:  # noqa: BLE001 — Temporal optional
        logger.debug("temporal server-probe ensure skipped", exc_info=True)
    return view


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
    # Stale non-URL client_id with use_cimd → rebind to the CIMD document URL.
    if (
        isinstance(client_id, str)
        and cfg.get("use_cimd")
        and not client_id.startswith("https://")
    ):
        client_id = None
    if not client_id:
        from .cimd import cimd_client_id

        # Prefer CIMD when asked (Vercel rejects unapproved DCR redirect URIs).
        # Also fall back to CIMD when DCR returns invalid_redirect_uri.
        prefer_cimd = bool(cfg.get("use_cimd"))
        reg = cfg.get("registration_endpoint")

        async def _bind_cimd() -> str:
            cid = cimd_client_id()
            if not cid:
                raise RegistryError(
                    422,
                    "invalid_oauth",
                    "CIMD requires PORTUNUS_PUBLIC_URL (https)",
                )
            await registry.update_server(
                server_id,
                actor=actor,
                oauth={
                    **{k: v for k, v in cfg.items() if k != "registration_endpoint"},
                    "client_id": cid,
                    "use_cimd": True,
                },
            )
            return cid

        if prefer_cimd:
            client_id = await _bind_cimd()
        elif isinstance(reg, str) and reg:
            try:
                registered = await register_oauth_client(
                    registration_endpoint=reg,
                    redirect_uri=redirect_uri,
                    scopes=list(cfg.get("scopes") or []),
                    token_endpoint_auth_method=str(
                        cfg.get("token_endpoint_auth_method") or "none"
                    ),
                )
            except OAuthError as e:
                # Vercel (and similar) advertise DCR then reject our redirect — use CIMD.
                if "invalid_redirect_uri" in (e.message or "") and cimd_client_id():
                    client_id = await _bind_cimd()
                else:
                    raise RegistryError(e.status, e.code, e.message) from e
            else:
                client_id = registered["client_id"]
                raw_secret = registered.get("client_secret")
                await registry.update_server(
                    server_id,
                    actor=actor,
                    oauth={**cfg, "client_id": client_id},
                    oauth_client_secret=raw_secret if isinstance(raw_secret, str) else None,
                )
        else:
            # Last resort: CIMD-capable gateway with no DCR endpoint on the server.
            if cimd_client_id():
                client_id = await _bind_cimd()
            else:
                raise RegistryError(422, "invalid_oauth", "oauth client_id is not configured")
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
    error_description: str = "",
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
    # RFC 9207 §2.4: validate iss before trusting the response. Some ASes omit iss on
    # error-only redirects; when iss is absent and there is no code, surface the AS
    # error (state still binds the flow). A present but wrong iss still masks errors.
    if pending is not None and (iss or code) and not iss_matches(iss, pending):
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
        detail = error
        if error_description:
            detail = f"{error}: {error_description}"
        return _oauth_fail_html(f"authorization server: {detail}", status=400)
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
        bundle = await exchange_code(
            pending,
            code,
            client_secret=secret,
            token_endpoint_auth_method=method,
        )
        store.discard(state)
        await registry.store_oauth_tokens(pending.server_id, bundle, actor="oauth-callback")
        # Proactive Temporal refresh workflow (no-op when Temporal is not configured).
        try:
            from portunus_worker.client import signal_oauth_tokens_stored

            await signal_oauth_tokens_stored(
                pending.server_id, expires_in=bundle.expires_in, has_refresh=bool(bundle.refresh_token)
            )
        except Exception:  # noqa: BLE001 — Temporal is optional at callback time
            logger.debug("temporal oauth signal skipped", exc_info=True)
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
    error_description: str = "",
) -> Response:
    """Browser redirect target after the upstream IdP (``response_mode=query``)."""
    return await _oauth_callback_result(
        request,
        code=code,
        state=state,
        error=error,
        iss=iss,
        error_description=error_description,
    )


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
        error_description=str(form.get("error_description") or ""),
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


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)
    username: str | None = Field(default=None, max_length=64)
    turnstile_token: str | None = Field(default=None, max_length=2048)


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    bootstrap_password: str | None = Field(default=None, max_length=1024)


class TotpLoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=80)


class PasswordChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current: str = Field(min_length=1, max_length=1024)
    next: str = Field(min_length=8, max_length=1024)


class TotpConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=6, max_length=8)


class PasskeyLoginBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=512)
    challenge: str = Field(min_length=1, max_length=256)
    response: dict[str, Any]


class PasskeyRegisterBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=64)
    challenge: str = Field(min_length=1, max_length=256)
    response: dict[str, Any]
    transports: list[str] | None = None


def _locked(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"code": "locked_out", "message": "too many failed attempts"},
        headers={"Retry-After": str(retry_after)},
    )


def _sf(request: Request):
    auth = getattr(request.app.state, "auth", None)
    if auth is not None:
        return auth.session_factory
    registry = getattr(request.app.state, "registry", None)
    return getattr(registry, "session_factory", None)


def _str_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _rp_id(request: Request) -> str:
    return urllib.parse.urlparse(str(request.url)).hostname or "localhost"


def _allowed_origins(_request: Request) -> set[str]:
    """WebAuthn origins from configured public URL / allowlist — never Host."""
    configured = set(allowed_origins())
    if configured:
        return configured
    # Local/dev fallback when neither PORTUNUS_PUBLIC_URL nor ALLOWED_ORIGINS is set.
    if os.environ.get("PORTUNUS_DEV") == "1":
        return {
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        }
    return set()


def _session_response(auth: OperatorAuth, token: str, body: dict[str, Any]) -> JSONResponse:
    out = JSONResponse(body)
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


async def _begin_session(
    auth: OperatorAuth,
    request: Request,
    *,
    username: str | None,
    detail: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    token, csrf, _sid = await auth.create_session()
    actor = username or OPERATOR
    await auth.audit(actor, "operator.login", {"ip": auth.client_ip(request), **detail})
    body: dict[str, Any] = {"csrf_token": csrf, "username": actor}
    if extra:
        body.update(extra)
    return _session_response(auth, token, body)


async def _require_turnstile(request: Request, token: str | None) -> None:
    if not turnstile_secret():
        return
    ip = _auth(request).client_ip(request)
    if not token_looks_plausible(token) or not await verify_login_token(token or "", remoteip=ip):
        raise RegistryError(403, "turnstile_failed", "human verification failed; refresh and try again")


def _passkey_view(row) -> dict[str, Any]:
    transports = row.transports.split(",") if row.transports else []
    return {
        "credential_id": row.credential_id,
        "name": row.name,
        "transports": transports,
        "created_at": _iso(row.created_at),
        "last_used_at": _iso(row.last_used_at),
        "sign_count": row.sign_count,
    }


@router.get("/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    sf = _sf(request)
    acct = await get_account(sf) if sf is not None else None
    n_keys = await count_passkeys(sf) if sf is not None and acct is not None else 0
    payload: dict[str, Any] = {
        "configured": acct is not None,
        "has_password": bool(acct and acct.password_hash),
        "has_totp": bool(acct and acct.totp_enabled),
        "has_passkey": n_keys > 0,
        "bootstrap_required": acct is None and bool(os.environ.get(PASSWORD_ENV)),
    }
    if acct is not None:
        payload["username"] = acct.username
    sitekey = turnstile_sitekey()
    if sitekey:
        payload["turnstile_sitekey"] = sitekey
    return payload


@router.post("/auth/setup")
async def auth_setup(body: SetupBody, request: Request):
    auth = _auth(request)
    sf = auth.session_factory
    if await get_account(sf) is not None:
        raise RegistryError(409, "already_configured", "the operator account already exists")
    username = body.username.strip()
    if not (3 <= len(username) <= 64):
        raise RegistryError(422, "invalid_request", "username must be 3 to 64 characters")
    if len(body.password) < 8:
        raise RegistryError(422, "invalid_request", "password must be at least 8 characters")
    env_pw = os.environ.get(PASSWORD_ENV) or ""
    if env_pw:
        supplied = body.bootstrap_password or ""
        if not _str_eq(supplied, env_pw):
            raise RegistryError(401, "unauthorized", "bootstrap password does not match")
    if not await create_account(sf, username, await auth.hash_password(body.password)):
        raise RegistryError(409, "already_configured", "the operator account already exists")
    await record_auth_event(sf, "setup", True, auth.client_ip(request), username)
    await auth.audit(username, "operator.setup", {})
    return await _begin_session(auth, request, username=username, detail={"method": "setup"})


@router.post("/auth/login")
async def login(body: LoginBody, request: Request):
    auth = _auth(request)
    sf = auth.session_factory
    acct = await get_account(sf)
    if acct is not None:
        return await _login_account(auth, request, body, acct)
    await _require_turnstile(request, body.turnstile_token)
    if not auth.password_hash:
        raise RegistryError(409, "setup_required", "no operator account — open setup first")
    ip = auth.client_ip(request)
    ok, retry_after = await auth.check_password(ip, body.password)
    if retry_after:
        return _locked(retry_after)
    if not ok:
        raise RegistryError(401, "unauthorized", "invalid password")
    return await _begin_session(auth, request, username=None, detail={})


async def _login_account(auth: OperatorAuth, request: Request, body: LoginBody, acct) -> JSONResponse:
    """Account password step. Env password alone is not accepted once configured."""
    await _require_turnstile(request, body.turnstile_token)
    sf = auth.session_factory
    ip = auth.client_ip(request)
    wait = await lockout_retry_after(sf, ip)
    if wait:
        await record_auth_event(sf, "lockout", False, ip, "login blocked")
        return _locked(wait)
    username = (body.username or "").strip()
    user_ok = bool(username) and _str_eq(acct.username, username)
    pw_ok = await auth.verify_password_hash(acct.password_hash, body.password)
    if not user_ok or not pw_ok:
        await record_auth_event(sf, "login_password", False, ip, (body.username or "")[:32])
        await auth.audit("anonymous", "operator.login_failed", {"ip": ip})
        raise RegistryError(401, "invalid_credentials", "wrong username or password")
    if acct.totp_enabled and acct.totp_secret:
        challenge = auth.issue_login_challenge(acct.username)
        await record_auth_event(sf, "login_password", True, ip, "totp step issued")
        return JSONResponse(
            status_code=401,
            content={
                "code": "totp_required",
                "challenge_token": challenge,
                "message": "enter your authenticator code",
            },
        )
    await record_auth_event(sf, "login_password", True, ip, acct.username)
    return await _begin_session(auth, request, username=acct.username, detail={"method": "password"})


@router.post("/auth/login/totp")
async def login_totp(body: TotpLoginBody, request: Request):
    auth = _auth(request)
    sf = auth.session_factory
    ip = auth.client_ip(request)
    wait = await lockout_retry_after(sf, ip)
    if wait:
        await record_auth_event(sf, "lockout", False, ip, "totp blocked")
        return _locked(wait)
    username = auth.login_challenge_username(body.challenge_token)
    if username is None:
        raise RegistryError(401, "invalid_challenge", "the challenge token is invalid or expired")
    acct = await get_account(sf)
    if acct is None or not acct.totp_enabled or not acct.totp_secret or not _str_eq(acct.username, username):
        raise RegistryError(409, "totp_not_enabled", "TOTP is not enabled on this account")
    raw = body.code.strip().replace(" ", "")
    if len(raw) == 6 and raw.isdigit():
        try:
            secret = open_totp_secret(acct.totp_secret)
        except OperatorCryptoError:
            await record_auth_event(sf, "login_totp", False, ip, "decrypt failed")
            raise RegistryError(401, "invalid_code", "wrong or replayed authenticator code") from None
        step = totp_verify_at(secret, raw, acct.totp_last_step)
        if step is None:
            await record_auth_event(sf, "login_totp", False, ip, "bad code")
            await auth.audit(username, "operator.login_failed", {"ip": ip, "step": "totp"})
            raise RegistryError(401, "invalid_code", "wrong or replayed authenticator code")
        # One row update wins the step. A lost race does not mint a session.
        if not await set_totp_step(sf, step):
            await record_auth_event(sf, "login_totp", False, ip, "replayed step")
            await auth.audit(username, "operator.login_failed", {"ip": ip, "step": "totp"})
            raise RegistryError(401, "invalid_code", "wrong or replayed authenticator code")
        # Challenge is single-use and must be spent before the session exists.
        if not auth.consume_login_challenge(body.challenge_token):
            await record_auth_event(sf, "login_totp", False, ip, "challenge spent")
            raise RegistryError(401, "invalid_challenge", "the challenge token is invalid or expired")
        await record_auth_event(sf, "login_totp", True, ip, username)
        return await _begin_session(auth, request, username=username, detail={"method": "totp"})
    remaining = await consume_recovery_code(sf, normalize_recovery_input(raw))
    if remaining is None:
        await record_auth_event(sf, "login_totp", False, ip, "bad recovery key")
        await auth.audit(username, "operator.login_failed", {"ip": ip, "step": "recovery"})
        raise RegistryError(401, "invalid_code", "wrong or already-used recovery key")
    if not auth.consume_login_challenge(body.challenge_token):
        await record_auth_event(sf, "login_totp", False, ip, "challenge spent")
        raise RegistryError(401, "invalid_challenge", "the challenge token is invalid or expired")
    await record_auth_event(sf, "login_recovery", True, ip, f"{username} ({remaining} left)")
    return await _begin_session(
        auth,
        request,
        username=username,
        detail={"method": "recovery"},
        extra={"recovery_remaining": remaining},
    )


@router.post("/auth/login/passkey/assertion/options")
async def passkey_assertion_options(request: Request) -> dict[str, Any]:
    auth = _auth(request)
    sf = auth.session_factory
    if await get_account(sf) is None:
        raise RegistryError(409, "setup_required", "no operator account")
    creds = await passkey_allows(sf)
    if not creds:
        raise RegistryError(409, "no_passkey", "no passkey is registered")
    return {
        "challenge": pk.new_challenge("assert"),
        "rpId": _rp_id(request),
        "allowCredentials": [{"type": "public-key", "id": cid} for cid, _transports in creds],
        "userVerification": "preferred",
        "timeout": pk.CHALLENGE_TTL_SEC * 1000,
    }


@router.post("/auth/login/passkey/verify")
async def passkey_login_verify(body: PasskeyLoginBody, request: Request):
    auth = _auth(request)
    sf = auth.session_factory
    ip = auth.client_ip(request)
    wait = await lockout_retry_after(sf, ip)
    if wait:
        await record_auth_event(sf, "lockout", False, ip, "passkey blocked")
        return _locked(wait)
    origins = _allowed_origins(request)
    resp = body.response or {}
    try:
        expected = pk.consume_challenge("assert", body.challenge)
    except pk.PasskeyError as exc:
        raise RegistryError(401, exc.code, exc.message) from None
    row = await get_passkey(sf, body.id)
    if row is None:
        await record_auth_event(sf, "login_passkey", False, ip, "unknown credential")
        raise RegistryError(401, "unknown_credential", "this passkey is not registered")
    try:
        new_count = pk.verify_assertion(
            credential_id_b64=body.id,
            client_data_b64=str(resp.get("clientDataJSON") or ""),
            authdata_b64=str(resp.get("authenticatorData") or ""),
            signature_b64=str(resp.get("signature") or ""),
            expected_challenge=expected,
            expected_rp_id=_rp_id(request),
            stored_public_key_hex=row.public_key,
            stored_sign_count=row.sign_count,
            stored_credential_id=row.credential_id,
            allowed_origins=origins,
        )
    except pk.PasskeyError as exc:
        await record_auth_event(sf, "login_passkey", False, ip, exc.code)
        await auth.audit(row.name, "operator.login_failed", {"ip": ip, "step": "passkey"})
        raise RegistryError(401, exc.code, exc.message) from None
    await touch_passkey(sf, row.credential_id, new_count)
    acct = await get_account(sf)
    username = acct.username if acct else OPERATOR
    await record_auth_event(sf, "login_passkey", True, ip, username)
    return await _begin_session(auth, request, username=username, detail={"method": "passkey"})


@router.get("/auth/session")
async def session_info(request: Request, actor: str = Depends(require_operator)) -> dict[str, Any]:
    auth = _auth(request)
    row = await auth.lookup_session(request.cookies.get(SESSION_COOKIE))
    acct = await get_account(auth.session_factory)
    return {
        "authenticated": True,
        "csrf_token": row.csrf_token if row else None,
        "username": acct.username if acct else actor,
    }


@router.post("/auth/step-up")
async def step_up(body: PasswordBody, request: Request, actor: str = Depends(require_operator)):
    auth = _auth(request)
    sf = auth.session_factory
    ip = auth.client_ip(request)
    acct = await get_account(sf)
    if acct is not None:
        wait = await lockout_retry_after(sf, ip)
        if wait:
            return _locked(wait)
        if not await auth.verify_password_hash(acct.password_hash, body.password):
            await record_auth_event(sf, "step_up", False, ip, actor)
            raise RegistryError(401, "unauthorized", "invalid password")
    else:
        ok, retry_after = await auth.check_password(ip, body.password)
        if retry_after:
            return _locked(retry_after)
        if not ok:
            raise RegistryError(401, "unauthorized", "invalid password")
    token = auth.issue_step_up(request.state.operator_session_id)
    await auth.audit(actor, "operator.step_up", {"ip": ip})
    return {"step_up_token": token}


@router.post("/auth/password")
async def change_password(
    body: PasswordChangeBody, request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    auth = _auth(request)
    sf = auth.session_factory
    acct = await get_account(sf)
    if acct is None or not acct.password_hash:
        raise RegistryError(409, "setup_required", "no operator password is set")
    if not await auth.verify_password_hash(acct.password_hash, body.current):
        raise RegistryError(401, "invalid_credentials", "current password is wrong")
    if not await set_password_hash(sf, await auth.hash_password(body.next)):
        raise RegistryError(409, "setup_required", "no operator password is set")
    await auth.audit(actor, "operator.password_changed", {})
    return {"ok": True}


@router.post("/auth/totp/enable")
async def totp_enable(request: Request, actor: str = Depends(require_step_up)) -> dict[str, Any]:
    acct = await get_account(_auth(request).session_factory)
    if acct is None:
        raise RegistryError(409, "setup_required", "no operator account")
    if acct.totp_enabled:
        raise RegistryError(409, "totp_enabled", "TOTP is already enabled")
    secret = fresh_totp_secret()
    return {"secret": secret, "provisioning_uri": totp_provisioning_uri(secret, acct.username)}


@router.post("/auth/totp/confirm")
async def totp_confirm(
    body: TotpConfirmBody, request: Request, actor: str = Depends(require_step_up)
) -> dict[str, Any]:
    auth = _auth(request)
    sf = auth.session_factory
    acct = await get_account(sf)
    if acct is None:
        raise RegistryError(409, "setup_required", "no operator account")
    if acct.totp_enabled:
        raise RegistryError(409, "totp_enabled", "TOTP is already enabled")
    code = body.code.strip().replace(" ", "")
    if totp_verify_at(body.secret, code, None) is None:
        raise RegistryError(401, "invalid_code", "wrong authenticator code")
    try:
        codes = generate_recovery_codes()
        await confirm_totp(sf, body.secret, codes)
    except LookupError:
        raise RegistryError(409, "setup_required", "no operator account") from None
    except OperatorCryptoError:
        raise RegistryError(500, "misconfigured", "PORTUNUS_SECRETS_KEY cannot seal the authenticator secret") from None
    await auth.audit(actor, "operator.totp_enabled", {})
    return {"ok": True, "codes": [c["code"] for c in codes]}


@router.post("/auth/totp/qr")
async def totp_qr(request: Request, actor: str = Depends(require_step_up)) -> dict[str, Any]:
    auth = _auth(request)
    acct = await get_account(auth.session_factory)
    if acct is None or not acct.totp_enabled or not acct.totp_secret:
        raise RegistryError(409, "totp_not_enabled", "TOTP is not enabled")
    try:
        secret = open_totp_secret(acct.totp_secret)
    except OperatorCryptoError:
        raise RegistryError(500, "misconfigured", "authenticator secret could not be decrypted") from None
    return {"secret": secret, "provisioning_uri": totp_provisioning_uri(secret, acct.username)}


@router.post("/auth/totp/disable")
async def totp_disable(
    body: PasswordBody, request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    auth = _auth(request)
    sf = auth.session_factory
    acct = await get_account(sf)
    if acct is None or not acct.totp_enabled:
        raise RegistryError(409, "totp_not_enabled", "TOTP is not enabled")
    if not await auth.verify_password_hash(acct.password_hash, body.password):
        await record_auth_event(sf, "totp_disable", False, auth.client_ip(request), actor)
        raise RegistryError(401, "invalid_credentials", "wrong password")
    await disable_totp(sf)
    await auth.audit(actor, "operator.totp_disabled", {})
    return {"ok": True}


@router.get("/auth/recovery")
async def recovery_status(request: Request, actor: str = Depends(require_operator)) -> dict[str, Any]:
    total, remaining = await recovery_counts(_auth(request).session_factory)
    return {"total": total or RECOVERY_COUNT, "remaining": remaining}


@router.post("/auth/recovery/regenerate")
async def recovery_regenerate(
    request: Request, actor: str = Depends(require_step_up)
) -> dict[str, Any]:
    auth = _auth(request)
    sf = auth.session_factory
    acct = await get_account(sf)
    if acct is None or not acct.totp_enabled:
        raise RegistryError(409, "totp_not_enabled", "enable two-factor first")
    try:
        codes = generate_recovery_codes()
        await replace_recovery_codes(sf, codes)
    except OperatorCryptoError:
        raise RegistryError(500, "misconfigured", "PORTUNUS_SECRETS_KEY cannot hash recovery codes") from None
    await auth.audit(actor, "operator.recovery_regenerated", {})
    return {"ok": True, "codes": [c["code"] for c in codes]}


@router.post("/auth/passkey/register/options")
async def passkey_register_options(request: Request, actor: str = Depends(require_step_up)) -> dict[str, Any]:
    auth = _auth(request)
    acct = await get_account(auth.session_factory)
    if acct is None:
        raise RegistryError(409, "setup_required", "no operator account")
    return {
        "challenge": pk.new_challenge("create"),
        "rp": {"name": "Portunus", "id": _rp_id(request)},
        "user": {
            "id": pk.b64url_encode(b"portunus-operator"),
            "name": acct.username,
            "displayName": acct.username,
        },
        "pubKeyCredParams": [
            {"type": "public-key", "alg": -7},
            {"type": "public-key", "alg": -257},
        ],
        "authenticatorSelection": {"residentKey": "preferred", "userVerification": "preferred"},
        "timeout": pk.CHALLENGE_TTL_SEC * 1000,
        "attestation": "none",
    }


@router.post("/auth/passkey/register/verify")
async def passkey_register_verify(
    body: PasskeyRegisterBody, request: Request, actor: str = Depends(require_step_up)
) -> dict[str, Any]:
    auth = _auth(request)
    name = body.name.strip()
    if not (1 <= len(name) <= 64):
        raise RegistryError(422, "invalid_request", "name must be 1 to 64 characters")
    origins = _allowed_origins(request)
    resp = body.response or {}
    try:
        expected = pk.consume_challenge("create", body.challenge)
        cred_id, pub_hex = pk.verify_registration(
            client_data_b64=str(resp.get("clientDataJSON") or ""),
            attestation_b64=str(resp.get("attestationObject") or ""),
            expected_challenge=expected,
            expected_rp_id=_rp_id(request),
            allowed_origins=origins,
        )
    except pk.PasskeyError as exc:
        await auth.audit(actor, "operator.passkey_register_failed", {"code": exc.code})
        raise RegistryError(400, exc.code, exc.message) from None
    transports = body.transports
    joined = ",".join(t for t in transports if isinstance(t, str)) if transports else None
    await save_passkey(
        auth.session_factory,
        credential_id=cred_id,
        public_key=pub_hex,
        name=name,
        transports=joined or None,
    )
    await auth.audit(actor, "operator.passkey_registered", {"name": name})
    return {"ok": True, "credential_id": cred_id}


@router.get("/auth/passkeys")
async def passkeys_list(request: Request, actor: str = Depends(require_operator)) -> dict[str, Any]:
    rows = await list_passkeys(_auth(request).session_factory)
    return {"passkeys": [_passkey_view(r) for r in rows]}


@router.delete("/auth/passkeys/{credential_id}")
async def passkey_delete(
    credential_id: str, request: Request, actor: str = Depends(require_step_up)
) -> dict[str, Any]:
    auth = _auth(request)
    deleted = await delete_passkey(auth.session_factory, credential_id)
    await auth.audit(actor, "operator.passkey_deleted", {"credential_id": credential_id[:24]})
    return {"ok": True, "deleted": deleted}


@router.get("/status")
async def operator_status(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    """Authenticated fleet status (public ``/healthz`` omits runner counts — review #8)."""
    pool = getattr(request.app.state, "runner_pool", None)
    connected = list(pool.connected()) if pool is not None else []
    temporal: dict[str, Any] = {"enabled": False}
    try:
        from portunus_worker.client import temporal_status

        temporal = await temporal_status()
    except Exception:  # noqa: BLE001
        temporal = {"enabled": False}
    return {
        "ok": True,
        "runner_connected": bool(connected),
        "runners_connected": len(connected),
        "temporal": temporal,
    }


@router.post("/servers/{server_id}/connect-ensure")
async def connect_ensure(
    server_id: uuid.UUID,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Start Connect/Ensure Temporal workflow (probe → enable → child probe/oauth)."""
    from .db.session import async_session as _async_session

    async with _async_session() as s:
        if await s.get(Server, server_id) is None:
            raise RegistryError(404, "not_found", "server not found")
    try:
        from portunus_worker.client import start_connect_ensure

        wid = await start_connect_ensure(server_id)
    except Exception as e:  # noqa: BLE001
        raise RegistryError(503, "unsupported", f"temporal unavailable: {e}") from e
    if wid is None:
        raise RegistryError(503, "unsupported", "TEMPORAL_HOST is not configured")
    await _audit(registry, actor, "server.connect_ensure", str(server_id), {"workflow_id": wid})
    return {"workflow_id": wid}


@router.post("/fleet/skills-sync")
async def fleet_skills_sync(
    request: Request,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Kick a fleet skill-tree sync workflow (hosts from body or PORTUNUS_FLEET_HOSTS)."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    hosts = body.get("hosts") if isinstance(body, dict) else None
    if hosts is not None and (
        not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts)
    ):
        raise RegistryError(422, "invalid_request", "hosts must be a list of strings")
    try:
        from portunus_worker.client import start_fleet_skills_sync

        wid = await start_fleet_skills_sync(hosts=list(hosts) if hosts else None)
    except Exception as e:  # noqa: BLE001
        raise RegistryError(503, "unsupported", f"temporal unavailable: {e}") from e
    if wid is None:
        raise RegistryError(503, "unsupported", "TEMPORAL_HOST is not configured")
    await _audit(registry, actor, "fleet.skills_sync", wid, {"hosts": hosts})
    return {"workflow_id": wid}


@router.post("/fleet/providers-update")
async def fleet_providers_update(
    request: Request,
    actor: str = Depends(require_operator),
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Update rclone, skill trees/CLIs, and skill_bridge on fleet hosts."""
    body: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:  # noqa: BLE001
        body = {}
    hosts = body.get("hosts")
    if hosts is not None and (
        not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts)
    ):
        raise RegistryError(422, "invalid_request", "hosts must be a list of strings")
    components = body.get("components")
    if components is not None and (
        not isinstance(components, list)
        or not all(isinstance(c, str) and c.strip() for c in components)
    ):
        raise RegistryError(
            422,
            "invalid_request",
            "components must be a list of strings (rclone, skills, skill_bridge)",
        )
    try:
        from portunus_worker.client import start_fleet_providers_update

        wid = await start_fleet_providers_update(
            hosts=list(hosts) if hosts else None,
            components=list(components) if components else None,
        )
    except Exception as e:  # noqa: BLE001
        raise RegistryError(503, "unsupported", f"temporal unavailable: {e}") from e
    if wid is None:
        raise RegistryError(503, "unsupported", "TEMPORAL_HOST is not configured")
    await _audit(
        registry,
        actor,
        "fleet.providers_update",
        wid,
        {"hosts": hosts, "components": components},
    )
    return {"workflow_id": wid}


@router.get("/temporal/status")
async def temporal_control_status(actor: str = Depends(require_operator)) -> dict[str, Any]:
    """Temporal connectivity for the Workflows console."""
    try:
        from portunus_worker.client import temporal_status

        return await temporal_status()
    except Exception as e:  # noqa: BLE001
        return {"enabled": False, "error": str(e)}


@router.get("/temporal/workflows")
async def temporal_list_workflows(
    actor: str = Depends(require_operator),
    query: str = "",
    page_size: int = 50,
    page_token: str = "",
) -> dict[str, Any]:
    """List control-plane workflows (Visibility query; defaults to running on Portunus queue)."""
    try:
        from portunus_worker.client import list_workflows

        return await list_workflows(
            query=query,
            page_size=page_size,
            page_token=page_token or None,
        )
    except Exception as e:  # noqa: BLE001
        raise RegistryError(503, "unsupported", f"temporal unavailable: {e}") from e


@router.get("/temporal/workflows/{workflow_id}")
async def temporal_describe_workflow(
    workflow_id: str,
    actor: str = Depends(require_operator),
    run_id: str = "",
    history_limit: int = 80,
) -> dict[str, Any]:
    """Describe one workflow and return a truncated history for the drawer."""
    try:
        from portunus_worker.client import describe_workflow

        return await describe_workflow(
            workflow_id,
            run_id=run_id or None,
            history_limit=min(max(history_limit, 1), 200),
        )
    except Exception as e:  # noqa: BLE001
        raise RegistryError(503, "unsupported", f"temporal unavailable: {e}") from e


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
            try:
                s.add(Grant(key_id=key_id, server_id=server_id))
                await s.commit()
            except IntegrityError:
                await s.rollback()
                # Unique (key_id, server_id) lost the race, or a parent row vanished.
                still = (
                    await s.execute(
                        select(Grant.id).where(Grant.key_id == key_id, Grant.server_id == server_id)
                    )
                ).first()
                if still is None:
                    raise RegistryError(404, "not_found", "key or server not found") from None
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

    @app.exception_handler(OAuthError)
    async def _oauth_error(_request: Request, exc: OAuthError) -> JSONResponse:
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
