"""OAuth PKCE helpers + operator paste-token / oauth start+callback paths."""

from __future__ import annotations

import os
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from portunus_gateway.db.models import VaultEntry
from portunus_gateway.db.session import async_session
from portunus_gateway.oauth import (
    AUTH_HEADER_NAME,
    OAUTH_CLIENT_SECRET_NAME,
    OAuthError,
    OAuthPending,
    OAuthStore,
    exchange_code,
    new_pkce,
    validate_oauth_config,
)
from portunus_gateway.operator_api import create_app, require_operator
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
        return httpx.Response(200, json={"access_token": "tok-xyz", "token_type": "Bearer"})

    pending = OAuthPending(
        server_id=uuid.uuid4(),
        code_verifier="verifier",
        token_url="https://idp.example/token",
        client_id="cid",
        redirect_uri="https://portunus.example/cb",
        created_at=0.0,
    )
    auth = await exchange_code(pending, "the-code", transport=httpx.MockTransport(handler))
    assert auth == "Bearer tok-xyz"
    assert seen and seen[0].url.host == "idp.example"
    body = parse_qs(seen[0].content.decode())
    assert body["code"] == ["the-code"]
    assert body["code_verifier"] == ["verifier"]
    assert body["client_id"] == ["cid"]


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

    async def fake_exchange(pending, code, *, client_secret=None, transport=None):
        assert code == "auth-code"
        assert pending.server_id == uuid.UUID(sid)
        return "Bearer from-oauth"

    monkeypatch.setattr("portunus_gateway.operator_api.exchange_code", fake_exchange)
    cb = await client.get("/api/oauth/callback", params={"code": "auth-code", "state": state})
    assert cb.status_code == 200
    assert "Connected" in cb.text

    listed = await client.get("/api/servers")
    row = next(s for s in listed.json() if s["id"] == sid)
    assert row["has_auth"] is True


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
