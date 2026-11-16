"""OAuth PKCE helpers + operator paste-token / oauth start+callback paths."""

from __future__ import annotations

import os
import uuid
from urllib.parse import parse_qs, urlparse

import json
import httpx
import pytest
from portunus_gateway.db.models import VaultEntry
from portunus_gateway.db.session import async_session
from portunus_gateway.oauth import (
    AUTH_HEADER_NAME,
    OAUTH_CLIENT_SECRET_NAME,
    OAUTH_REFRESH_TOKEN_NAME,
    OAuthError,
    OAuthPending,
    OAuthStore,
    exchange_code,
    new_pkce,
    refresh_access_token,
    validate_oauth_config,
)
from portunus_gateway.operator_api import create_app, require_operator, require_step_up
from portunus_gateway.registry import Registry
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.vault import Vault
from sqlalchemy import select

pytestmark = pytest.mark.anyio


def _vault() -> Vault:
    return Vault({1: os.urandom(32)})


def _app(registry: Registry):
    app = create_app(registry)
    app.dependency_overrides[require_operator] = lambda: "test-operator"
    app.dependency_overrides[require_step_up] = lambda: "test-operator"
    return app


@pytest.fixture
async def http(db):
    vault = _vault()
    registry = Registry(
        async_session,
        vault,
        RunnerPool([]),
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
    app = _app(registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, registry, app


def test_new_pkce_s256_shape():
    verifier, challenge = new_pkce()
    assert len(verifier) >= 43 and "=" not in verifier
    assert len(challenge) >= 43 and "=" not in challenge
    assert verifier != challenge


def test_validate_oauth_config_normalizes_and_rejects():
    assert validate_oauth_config(None) is None
    assert validate_oauth_config({}) is None
    ok = validate_oauth_config(
        {
            "authorize_url": " https://idp.example/authorize ",
            "token_url": "https://idp.example/token",
            "client_id": "cid",
            "scopes": ["mcp", "openid"],
        }
    )
    assert ok == {
        "authorize_url": "https://idp.example/authorize",
        "token_url": "https://idp.example/token",
        "client_id": "cid",
        "scopes": ["mcp", "openid"],
    }
    dcr = validate_oauth_config(
        {
            "authorize_url": "https://ardot.tencent.com/oauth/authorize",
            "token_url": "https://ardot.tencent.com/oauth/token",
            "scopes": ["mcp:use"],
            "resource": "https://ardot.tencent.com/mcp",
            "registration_endpoint": "https://ardot.tencent.com/oauth/register",
            "token_endpoint_auth_method": "none",
        }
    )
    assert dcr is not None and "client_id" not in dcr
    assert dcr["resource"] == "https://ardot.tencent.com/mcp"
    with pytest.raises(OAuthError) as exc:
        validate_oauth_config({"authorize_url": "http://insecure", "token_url": "https://t", "client_id": "c"})
    assert exc.value.code == "invalid_oauth"


def test_oauth_store_builds_authorize_url_and_expires():
    store = OAuthStore(ttl_s=0.01)
    sid = uuid.uuid4()
    started = store.start(
        server_id=sid,
        authorize_url="https://idp.example/auth",
        token_url="https://idp.example/token",
        client_id="cid",
        scopes=["a", "b"],
        redirect_uri="https://portunus.example/api/oauth/callback",
    )
    parsed = urlparse(started["authorize_url"])
    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["scope"] == ["a b"]
    assert qs["state"] == [started["state"]]
    pending = store.pop(started["state"])
    assert pending.server_id == sid
    with pytest.raises(OAuthError):
        store.pop(started["state"])


async def test_exchange_code_posts_pkce_and_formats_authorization():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "tok-xyz",
                "token_type": "Bearer",
                "refresh_token": "rt-abc",
                "expires_in": 7200,
            },
        )

    pending = OAuthPending(
        server_id=uuid.uuid4(),
        code_verifier="verifier",
        token_url="https://idp.example/token",
        client_id="cid",
        redirect_uri="https://portunus.example/cb",
        created_at=0.0,
    )
    bundle = await exchange_code(pending, "the-code", transport=httpx.MockTransport(handler))
    assert bundle.authorization == "Bearer tok-xyz"
    assert bundle.refresh_token == "rt-abc"
    assert bundle.expires_in == 7200
    assert seen and seen[0].url.host == "idp.example"
    body = parse_qs(seen[0].content.decode())
    assert body["code"] == ["the-code"]
    assert body["code_verifier"] == ["verifier"]
    assert body["client_id"] == ["cid"]


async def test_refresh_access_token_keeps_prior_refresh_when_omitted():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "new-tok", "token_type": "Bearer", "expires_in": 60})

    bundle = await refresh_access_token(
        token_url="https://idp.example/token",
        client_id="cid",
        refresh_token="rt-old",
        transport=httpx.MockTransport(handler),
    )
    assert bundle.authorization == "Bearer new-tok"
    assert bundle.refresh_token == "rt-old"
    assert bundle.expires_in == 60
    body = parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["rt-old"]


async def test_create_remote_with_oauth_and_paste_token(http):
    client, registry, _app = http
    r = await client.post(
        "/api/servers",
        json={
            "name": "Remote OAuth",
            "slug": "remote-oauth",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "client_id": "cid",
                "scopes": ["mcp"],
            },
            "oauth_client_secret": "super-secret",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["oauth"]["configured"] is True
    assert body["has_auth"] is False
    assert "Authorization" not in body["env_names"]
    # reserved vault name hidden from env_names
    assert all(not n.lower().startswith("portunus.") for n in body["env_names"])

    sid = uuid.UUID(body["id"])
    secret = await registry.read_oauth_client_secret(sid)
    assert secret == "super-secret"

    tr = await client.post(f"/api/servers/{sid}/auth/token", json={"token": "paste-me"})
    assert tr.status_code == 200, tr.text
    assert tr.json()["has_auth"] is True
    assert "Authorization" in tr.json()["env_names"]

    async with async_session() as s:
        row = (
            await s.execute(
                select(VaultEntry).where(
                    VaultEntry.server_id == sid, VaultEntry.name == AUTH_HEADER_NAME
                )
            )
        ).scalar_one()
        plain = registry.vault.open(
            bytes(row.ciphertext), vault_entry_id=str(row.id), key_version=row.key_version
        ).decode()
        assert plain == "Bearer paste-me"
        assert (
            await s.execute(
                select(VaultEntry).where(
                    VaultEntry.server_id == sid, VaultEntry.name == OAUTH_CLIENT_SECRET_NAME
                )
            )
        ).scalar_one_or_none() is not None


async def test_oauth_start_and_callback_stores_token(http, monkeypatch):
    client, registry, app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "R",
            "slug": "r-oauth-flow",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "client_id": "cid",
                "scopes": [],
            },
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    redirect = "http://test/api/oauth/callback"
    start = await client.post(f"/api/servers/{sid}/oauth/start", json={"redirect_uri": redirect})
    assert start.status_code == 200, start.text
    state = start.json()["state"]
    authorize_url = start.json()["authorize_url"]
    assert "code_challenge=" in authorize_url and state in authorize_url

    async def fake_exchange(
        pending, code, *, client_secret=None, token_endpoint_auth_method=None, transport=None
    ):
        from portunus_gateway.oauth import TokenBundle

        assert code == "auth-code"
        assert pending.server_id == uuid.UUID(sid)
        return TokenBundle(
            authorization="Bearer from-oauth", refresh_token="rt-1", expires_in=3600
        )

    monkeypatch.setattr("portunus_gateway.operator_api.exchange_code", fake_exchange)
    cb = await client.get("/api/oauth/callback", params={"code": "auth-code", "state": state})
    assert cb.status_code == 200
    assert "Connected" in cb.text

    listed = await client.get("/api/servers")
    row = next(s for s in listed.json() if s["id"] == sid)
    assert row["has_auth"] is True

    async with async_session() as s:
        refresh_row = (
            await s.execute(
                select(VaultEntry).where(
                    VaultEntry.server_id == uuid.UUID(sid),
                    VaultEntry.name == OAUTH_REFRESH_TOKEN_NAME,
                )
            )
        ).scalar_one()
        plain = registry.vault.open(
            bytes(refresh_row.ciphertext),
            vault_entry_id=str(refresh_row.id),
            key_version=refresh_row.key_version,
        ).decode()
        assert plain == "rt-1"


async def test_oauth_start_requires_config(http):
    client, _registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "Plain remote",
            "slug": "plain-remote",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
        },
    )
    sid = created.json()["id"]
    r = await client.post(
        f"/api/servers/{sid}/oauth/start",
        json={"redirect_uri": "https://portunus.example/api/oauth/callback"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_oauth"


async def test_patch_remote_oauth_config_and_secret(http):
    client, registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "Plain remote",
            "slug": "patch-oauth",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    r = await client.patch(
        f"/api/servers/{sid}",
        json={
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "client_id": "new-cid",
                "scopes": ["files"],
            },
            "oauth_client_secret": "rotated-secret",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["oauth"]["configured"] is True
    assert body["oauth"]["client_id"] == "new-cid"
    assert body["oauth"]["scopes"] == ["files"]
    assert await registry.read_oauth_client_secret(uuid.UUID(sid)) == "rotated-secret"


# ---- RFC 9207 iss validation + DCR application_type (audit M4) ---------------------------


def test_iss_matches_semantics():
    pending = OAuthPending(
        server_id=uuid.uuid4(),
        code_verifier="v",
        token_url="https://idp.example/token",
        client_id="cid",
        redirect_uri="https://portunus.example/cb",
        created_at=0.0,
        issuer_origin="https://idp.example",
    )
    from portunus_gateway.oauth import iss_matches

    # Absent iss: cannot be verified without AS metadata — accepted.
    assert iss_matches("", pending) is True
    assert iss_matches(None, pending) is True
    # Origin anchor: same authority passes, tenant paths tolerated.
    assert iss_matches("https://idp.example", pending) is True
    assert iss_matches("https://idp.example/tenant1", pending) is True
    # Authority normalization (RFC 3986 6.2.3): case + explicit default port.
    assert iss_matches("https://IDP.example", pending) is True
    assert iss_matches("https://idp.example:443", pending) is True
    # Different authority / unparseable → rejected.
    assert iss_matches("https://evil.example", pending) is False
    assert iss_matches("not a url", pending) is False

    exact = OAuthPending(
        server_id=uuid.uuid4(),
        code_verifier="v",
        token_url="https://idp.example/token",
        client_id="cid",
        redirect_uri="https://portunus.example/cb",
        created_at=0.0,
        issuer="https://idp.example/tenant1",
        issuer_origin="https://idp.example",
    )
    # Explicit issuer → exact string comparison, no path tolerance; missing iss rejected.
    assert iss_matches("https://idp.example/tenant1", exact) is True
    assert iss_matches("https://idp.example", exact) is False
    assert iss_matches("", exact) is False
    assert iss_matches(None, exact) is False


def test_store_start_records_issuer_anchor():
    from portunus_gateway.oauth import OAuthStore

    store = OAuthStore()
    started = store.start(
        server_id=uuid.uuid4(),
        authorize_url="https://idp.example/authorize",
        token_url="https://idp.example/token",
        client_id="cid",
        scopes=[],
        redirect_uri="https://portunus.example/cb",
        issuer="https://issuer.example/real",
    )
    pending = store.pop(started["state"])
    assert pending.issuer == "https://issuer.example/real"
    assert pending.issuer_origin == "https://idp.example"


async def test_dcr_body_includes_application_type():
    from portunus_gateway.oauth import register_oauth_client

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"client_id": "reg-1"})

    out = await register_oauth_client(
        registration_endpoint="https://idp.example/register",
        redirect_uri="https://portunus.example/api/oauth/callback",
        transport=httpx.MockTransport(handler),
    )
    assert out["client_id"] == "reg-1"
    body = json.loads(seen[0].content)
    assert body["application_type"] == "web"
    # Plain-http redirect (dev dashboard / loopback) fits the OIDC native profile instead.
    seen.clear()
    await register_oauth_client(
        registration_endpoint="https://idp.example/register",
        redirect_uri="http://127.0.0.1:8080/api/oauth/callback",
        transport=httpx.MockTransport(handler),
    )
    assert json.loads(seen[0].content)["application_type"] == "native"


async def test_callback_rejects_iss_mismatch_without_exchange(http, monkeypatch):
    client, _registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "R",
            "slug": "iss-mismatch",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "client_id": "cid",
                "scopes": [],
            },
        },
    )
    sid = created.json()["id"]
    start = await client.post(
        f"/api/servers/{sid}/oauth/start", json={"redirect_uri": "http://test/api/oauth/callback"}
    )
    state = start.json()["state"]

    async def must_not_exchange(*a, **kw):  # pragma: no cover - fails the test if reached
        raise AssertionError("code must not be exchanged on iss mismatch")

    monkeypatch.setattr("portunus_gateway.operator_api.exchange_code", must_not_exchange)
    r = await client.get(
        "/api/oauth/callback",
        params={"code": "c", "state": state, "iss": "https://evil.example"},
    )
    assert r.status_code == 400
    assert "issuer (iss) mismatch" in r.text
    # State is consumed: a replay with a good iss still cannot exchange.
    r2 = await client.get(
        "/api/oauth/callback",
        params={"code": "c", "state": state, "iss": "https://idp.example"},
    )
    assert r2.status_code == 400
    assert "unknown or expired oauth state" in r2.text


async def test_callback_iss_mismatch_masks_error_params(http, monkeypatch):
    """RFC 9207: on mismatch the error params must not be shown or acted on."""
    client, _registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "R",
            "slug": "iss-error-mask",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "client_id": "cid",
                "scopes": [],
            },
        },
    )
    sid = created.json()["id"]
    start = await client.post(
        f"/api/servers/{sid}/oauth/start", json={"redirect_uri": "http://test/api/oauth/callback"}
    )
    state = start.json()["state"]
    r = await client.get(
        "/api/oauth/callback",
        params={
            "state": state,
            "error": "access_denied",
            "error_description": "secret-hint",
            "iss": "https://evil.example",
        },
    )
    assert r.status_code == 400
    assert "issuer (iss) mismatch" in r.text
    assert "access_denied" not in r.text
    assert "secret-hint" not in r.text


async def test_callback_error_without_iss_surfaces_as_error(http):
    """ASes often omit iss on error redirects; with an explicit issuer still show the error."""
    client, _registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "R",
            "slug": "iss-error-omit",
            "kind": "remote",
            "url": "https://mcp.vultr.com/mcp",
            "oauth": {
                "authorize_url": "https://mcp.vultr.com/authorize",
                "token_url": "https://mcp.vultr.com/token",
                "client_id": "cid",
                "issuer": "https://mcp.vultr.com/",
                "resource": "https://mcp.vultr.com/",
                "scopes": [],
            },
        },
    )
    sid = created.json()["id"]
    start = await client.post(
        f"/api/servers/{sid}/oauth/start", json={"redirect_uri": "http://test/api/oauth/callback"}
    )
    state = start.json()["state"]
    r = await client.get(
        "/api/oauth/callback",
        params={
            "state": state,
            "error": "server_error",
            "error_description": "An unexpected error occurred",
        },
    )
    assert r.status_code == 400
    assert "server_error" in r.text
    assert "unexpected error" in r.text
    assert "iss) mismatch" not in r.text


async def test_callback_code_without_iss_ok_without_configured_issuer(http, monkeypatch):
    """Vultr-style: no catalog issuer, AS omits iss — origin anchor must accept the code."""
    client, _registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "R",
            "slug": "iss-omit-ok",
            "kind": "remote",
            "url": "https://mcp.vultr.com/mcp",
            "oauth": {
                "authorize_url": "https://mcp.vultr.com/authorize",
                "token_url": "https://mcp.vultr.com/token",
                "client_id": "cid",
                "resource": "https://mcp.vultr.com/",
                "scopes": [],
            },
        },
    )
    sid = created.json()["id"]
    start = await client.post(
        f"/api/servers/{sid}/oauth/start", json={"redirect_uri": "http://test/api/oauth/callback"}
    )
    state = start.json()["state"]

    async def fake_exchange(pending, code, **kw):
        from portunus_gateway.oauth import TokenBundle

        assert code == "vultr-code"
        assert not pending.issuer
        return TokenBundle(authorization="Bearer atok", refresh_token="rtok", expires_in=3600)

    monkeypatch.setattr("portunus_gateway.operator_api.exchange_code", fake_exchange)
    r = await client.get(
        "/api/oauth/callback",
        params={"code": "vultr-code", "state": state},
    )
    assert r.status_code == 200
    assert "Connected" in r.text


async def test_callback_accepts_matching_iss(http, monkeypatch):
    client, registry, _app = http
    created = await client.post(
        "/api/servers",
        json={
            "name": "R",
            "slug": "iss-ok",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "client_id": "cid",
                "scopes": [],
            },
        },
    )
    sid = created.json()["id"]
    start = await client.post(
        f"/api/servers/{sid}/oauth/start", json={"redirect_uri": "http://test/api/oauth/callback"}
    )
    state = start.json()["state"]

    async def fake_exchange(
        pending, code, *, client_secret=None, token_endpoint_auth_method=None, transport=None
    ):
        from portunus_gateway.oauth import TokenBundle

        return TokenBundle(authorization="Bearer ok")

    monkeypatch.setattr("portunus_gateway.operator_api.exchange_code", fake_exchange)
    r = await client.get(
        "/api/oauth/callback",
        params={"code": "c", "state": state, "iss": "https://idp.example/tenant7"},
    )
    assert r.status_code == 200
    assert "Connected" in r.text


def test_catalog_spec_round_trips_issuer():
    """The catalog allowlist must keep spec.issuer or the dashboard field is dead on save."""
    from portunus_gateway.catalog import validate_spec

    spec = validate_spec(
        "oauth",
        {
            "url": "https://mcp.example.com/mcp",
            "authorize_url": "https://idp.example/authorize",
            "token_url": "https://idp.example/token",
            "scopes": [],
            "issuer": "https://issuer.example/real",
        },
    )
    assert spec["issuer"] == "https://issuer.example/real"
    import pytest as _pytest

    with _pytest.raises(Exception):
        validate_spec(
            "oauth",
            {
                "url": "https://mcp.example.com/mcp",
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "scopes": [],
                "issuer": "idp.example",
            },
        )


def test_catalog_oauth_endpoints_require_https():
    from portunus_gateway.catalog import validate_spec
    from portunus_gateway.registry import RegistryError

    base = {
        "url": "https://mcp.example.com/mcp",
        "authorize_url": "https://idp.example/authorize",
        "token_url": "https://idp.example/token",
        "scopes": [],
    }
    for key in ("url", "authorize_url", "token_url"):
        bad = dict(base)
        bad[key] = "http://idp.example/plain"
        with pytest.raises(RegistryError) as exc:
            validate_spec("oauth", bad)
        assert "https" in exc.value.message


async def test_token_endpoint_auth_method_selects_basic_or_post():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer"})

    pending = OAuthPending(
        server_id=uuid.uuid4(),
        code_verifier="verifier",
        token_url="https://idp.example/token",
        client_id="cid",
        redirect_uri="https://portunus.example/cb",
        created_at=0.0,
    )
    transport = httpx.MockTransport(handler)

    await exchange_code(
        pending,
        "the-code",
        client_secret="sekret",
        token_endpoint_auth_method="client_secret_post",
        transport=transport,
    )
    posted = parse_qs(seen[-1].content.decode())
    assert posted["client_secret"] == ["sekret"]
    assert "authorization" not in {k.lower() for k in seen[-1].headers}

    await exchange_code(
        pending,
        "the-code",
        client_secret="sekret",
        token_endpoint_auth_method="client_secret_basic",
        transport=transport,
    )
    basic = parse_qs(seen[-1].content.decode())
    assert "client_secret" not in basic
    assert seen[-1].headers["authorization"].startswith("Basic ")

    await exchange_code(
        pending,
        "the-code",
        client_secret="sekret",
        token_endpoint_auth_method="none",
        transport=transport,
    )
    public = parse_qs(seen[-1].content.decode())
    assert "client_secret" not in public
    assert "authorization" not in {k.lower() for k in seen[-1].headers}

    await refresh_access_token(
        token_url="https://idp.example/token",
        client_id="cid",
        refresh_token="rt",
        client_secret="sekret",
        token_endpoint_auth_method="client_secret_post",
        transport=transport,
    )
    refreshed = parse_qs(seen[-1].content.decode())
    assert refreshed["grant_type"] == ["refresh_token"]
    assert refreshed["client_secret"] == ["sekret"]
    assert "authorization" not in {k.lower() for k in seen[-1].headers}

    await refresh_access_token(
        token_url="https://idp.example/token",
        client_id="cid",
        refresh_token="rt",
        client_secret="sekret",
        token_endpoint_auth_method="client_secret_basic",
        transport=transport,
    )
    assert "client_secret" not in parse_qs(seen[-1].content.decode())
    assert seen[-1].headers["authorization"].startswith("Basic ")


async def test_dcr_persists_client_secret(http, monkeypatch):
    client, registry, _app = http

    async def fake_register(**kwargs):
        assert kwargs["token_endpoint_auth_method"] == "client_secret_post"
        return {"client_id": "dyn-1", "client_secret": "dyn-secret"}

    monkeypatch.setattr("portunus_gateway.operator_api.register_oauth_client", fake_register)
    created = await client.post(
        "/api/servers",
        json={
            "name": "DCR",
            "slug": "dcr-secret",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "oauth": {
                "authorize_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "scopes": ["mcp"],
                "registration_endpoint": "https://idp.example/register",
                "token_endpoint_auth_method": "client_secret_post",
            },
        },
    )
    assert created.status_code == 201, created.text
    sid = uuid.UUID(created.json()["id"])
    start = await client.post(
        f"/api/servers/{sid}/oauth/start",
        json={"redirect_uri": "http://test/api/oauth/callback"},
    )
    assert start.status_code == 200, start.text
    assert await registry.read_oauth_client_secret(sid) == "dyn-secret"
    cfg = await registry.remote_oauth_config(sid)
    assert cfg["client_id"] == "dyn-1"
