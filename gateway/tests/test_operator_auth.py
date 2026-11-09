"""Operator auth (Task 12): argon2id password, opaque session cookie, CSRF, step-up, lockout."""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from argon2 import PasswordHasher
from portunus_gateway import authz as authz_mod
from portunus_gateway.db.models import AgentKey, AuditEvent, Grant, OperatorSession, Server
from portunus_gateway.db.session import async_session
from portunus_gateway.operator_api import create_app
from portunus_gateway.operator_auth import OperatorAuth
from portunus_gateway.registry import Registry
from portunus_gateway.vault import Vault
from sqlalchemy import select

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery staple"
# Cheap argon2id parameters: the algorithm is the real one, only the cost is test-sized.
_FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _registry() -> Registry:
    return Registry(
        async_session,
        Vault({1: os.urandom(32)}),
        object(),
        trusted_path=[],
        binary_allowlist=[],
    )


def _auth(clock: Clock | None = None, **kw) -> OperatorAuth:
    kw.setdefault("secure_cookie", False)
    return OperatorAuth.from_password(
        PASSWORD, session_factory=async_session, hasher=_FAST, clock=clock or Clock(), **kw
    )


@contextlib.asynccontextmanager
async def _client(auth: OperatorAuth, *, ip: str = "10.0.0.1"):
    app = create_app(_registry(), auth=auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://test"
    ) as c:
        yield c


async def _login(c: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await c.post("/api/auth/login", json={"password": password})


async def _logged_in(c: httpx.AsyncClient) -> dict[str, str]:
    r = await _login(c)
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


# ---- lockout (the failing test written first) -----------------------------------------


async def test_five_failed_logins_lock_out_ip_for_15_minutes(db):
    clock = Clock()
    async with _client(_auth(clock)) as c:
        for _ in range(5):
            r = await _login(c, "wrong")
            assert r.status_code == 401
        # Locked: even the correct password is refused, with a Retry-After.
        r = await _login(c)
        assert r.status_code == 429
        assert r.json()["code"] == "locked_out"
        assert 0 < int(r.headers["retry-after"]) <= 900

        clock.now += 14 * 60
        assert (await _login(c)).status_code == 429
        clock.now += 61  # 15 min + 1 s since the lock
        assert (await _login(c)).status_code == 200


async def test_lockout_is_per_ip(db):
    auth = _auth()
    async with _client(auth, ip="10.0.0.1") as bad, _client(auth, ip="10.0.0.2") as good:
        for _ in range(5):
            await _login(bad, "wrong")
        assert (await _login(bad)).status_code == 429
        assert (await _login(good)).status_code == 200


async def test_four_failures_then_success_resets_counter(db):
    async with _client(_auth()) as c:
        for _ in range(4):
            assert (await _login(c, "wrong")).status_code == 401
        assert (await _login(c)).status_code == 200
        for _ in range(4):
            assert (await _login(c, "wrong")).status_code == 401
        assert (await _login(c)).status_code == 200


async def test_failed_step_up_counts_toward_lockout(db):
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        for _ in range(5):
            r = await c.post("/api/auth/step-up", json={"password": "wrong"}, headers=csrf)
            assert r.status_code == 403
        r = await c.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf)
        assert r.status_code == 429


async def test_xff_ignored_unless_peer_is_trusted_proxy(db):
    # Untrusted peer: rotating X-Forwarded-For must not dodge the lockout.
    async with _client(_auth()) as c:
        for i in range(5):
            await c.post(
                "/api/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": f"9.9.9.{i}"}
            )
        r = await c.post(
            "/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "9.9.9.99"}
        )
        assert r.status_code == 429
    # Trusted proxy: the forwarded client address is what gets locked.
    auth = _auth(trusted_proxies=["10.0.0.1"])
    async with _client(auth) as c:
        for _ in range(5):
            await c.post(
                "/api/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": "7.7.7.7"}
            )
        locked = await c.post(
            "/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "7.7.7.7"}
        )
        other = await c.post(
            "/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "7.7.7.8"}
        )
        assert locked.status_code == 429
        assert other.status_code == 200


# ---- password / session ---------------------------------------------------------------


def test_password_from_env_is_hashed_argon2id(monkeypatch):
    monkeypatch.setenv("PORTUNUS_OPERATOR_PASSWORD", PASSWORD)
    auth = OperatorAuth.from_env()
    assert auth.password_hash.startswith("$argon2id$")
    assert PASSWORD not in repr(vars(auth))


def test_missing_password_env_fails_closed(monkeypatch):
    monkeypatch.delenv("PORTUNUS_OPERATOR_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        OperatorAuth.from_env()


async def test_unauthenticated_requests_denied(db):
    async with _client(_auth()) as c:
        assert (await c.get("/api/servers")).status_code == 401
        assert (await c.get("/api/keys")).status_code == 401
        assert (await c.post("/api/keys", json={})).status_code == 401


async def test_no_auth_configured_denies_everything(db):
    app = create_app(_registry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/servers")).status_code == 401
        assert (await c.post("/api/auth/login", json={"password": PASSWORD})).status_code == 401


async def test_login_sets_opaque_httponly_samesite_secure_cookie(db):
    async with _client(_auth(secure_cookie=True)) as c:
        r = await _login(c)
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        low = cookie.lower()
        assert cookie.startswith("portunus_session=")
        assert "httponly" in low and "secure" in low and "samesite=strict" in low
        token = cookie.split("=", 1)[1].split(";", 1)[0]
        assert len(token) >= 40
    # The raw token is never stored, only its hash.
    async with async_session() as s:
        rows = (await s.execute(select(OperatorSession))).scalars().all()
    assert len(rows) == 1
    assert token.encode() not in rows[0].token_hash and len(rows[0].token_hash) == 32


async def test_session_roundtrip_and_logout(db):
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        assert (await c.get("/api/servers")).status_code == 200
        assert (await c.get("/api/auth/session")).json()["csrf_token"] == csrf["X-CSRF-Token"]
        assert (await c.post("/api/auth/logout", headers=csrf)).status_code == 204
        assert (await c.get("/api/servers")).status_code == 401
    async with async_session() as s:
        assert (await s.execute(select(OperatorSession))).first() is None


async def test_forged_and_expired_sessions_rejected(db):
    async with _client(_auth()) as c:
        c.cookies.set("portunus_session", "A" * 43)
        assert (await c.get("/api/servers")).status_code == 401
    async with _client(_auth()) as c:
        await _logged_in(c)
        async with async_session() as s:
            row = (await s.execute(select(OperatorSession))).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await s.commit()
        assert (await c.get("/api/servers")).status_code == 401


# ---- CSRF -----------------------------------------------------------------------------


async def test_mutations_require_csrf_token(db, pepper_file, authz):
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        body = {"label": "agent"}
        assert (await c.post("/api/keys", json=body)).status_code == 403
        r = await c.post("/api/keys", json=body, headers={"X-CSRF-Token": "nope"})
        assert r.status_code == 403 and r.json()["code"] == "csrf"
        assert (await c.post("/api/auth/logout")).status_code == 403
        assert (await c.post("/api/keys", json=body, headers=csrf)).status_code == 201
        # Reads need only the cookie.
        assert (await c.get("/api/keys")).status_code == 200


# ---- step-up --------------------------------------------------------------------------


async def test_step_up_token_flow(db):
    clock = Clock()
    auth = _auth(clock)
    async with _client(auth) as c:
        csrf = await _logged_in(c)
        sid = uuid.uuid4()
        # Wrong password -> no token.
        r = await c.post("/api/auth/step-up", json={"password": "bad"}, headers=csrf)
        assert r.status_code == 403
        r = await c.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf)
        assert r.status_code == 200
        step = r.json()["token"]
        assert r.json()["expires_in"] == 300

        # Gated route: needs the token.
        url = f"/api/servers/{sid}/enable"
        r = await c.post(url, json={}, headers=csrf)
        assert r.status_code == 403 and r.json()["code"] == "step_up_required"
        r = await c.post(url, json={}, headers={**csrf, "X-Step-Up-Token": "bogus"})
        assert r.status_code == 403
        r = await c.post(url, json={}, headers={**csrf, "X-Step-Up-Token": step})
        assert r.status_code == 404  # authorized; the server just doesn't exist

        # Expires after 5 minutes.
        clock.now += 301
        r = await c.post(url, json={}, headers={**csrf, "X-Step-Up-Token": step})
        assert r.status_code == 403


async def test_step_up_token_bound_to_session(db):
    auth = _auth()
    async with _client(auth) as a, _client(auth) as b:
        csrf_a = await _logged_in(a)
        csrf_b = await _logged_in(b)
        step = (
            await a.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf_a)
        ).json()["token"]
        r = await b.post(
            f"/api/servers/{uuid.uuid4()}/enable",
            json={},
            headers={**csrf_b, "X-Step-Up-Token": step},
        )
        assert r.status_code == 403


async def test_step_up_token_dies_with_logout(db):
    auth = _auth()
    async with _client(auth) as c:
        csrf = await _logged_in(c)
        step = (
            await c.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf)
        ).json()["token"]
        await c.post("/api/auth/logout", headers=csrf)
        csrf2 = await _logged_in(c)
        r = await c.post(
            f"/api/servers/{uuid.uuid4()}/enable",
            json={},
            headers={**csrf2, "X-Step-Up-Token": step},
        )
        assert r.status_code == 403


# ---- key REST -------------------------------------------------------------------------


async def test_mint_list_revoke_key(db, pepper_file, authz):
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        r = await c.post("/api/keys", json={"label": "agent-1"}, headers=csrf)
        assert r.status_code == 201
        minted = r.json()
        assert minted["token"].startswith("pm-") and minted["token"].split(".")[0][3:] == minted["key_id"]

        listed = (await c.get("/api/keys")).json()
        assert [k["key_id"] for k in listed] == [minted["key_id"]]
        assert "token" not in listed[0] and "digest" not in listed[0]
        assert minted["token"].split(".")[1] not in str(listed)
        assert listed[0]["revoked_at"] is None and listed[0]["server_ids"] == []

        # Revoke is step-up gated.
        url = f"/api/keys/{minted['key_id']}/revoke"
        assert (await c.post(url, headers=csrf)).status_code == 403
        step = (
            await c.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf)
        ).json()["token"]
        r = await c.post(url, headers={**csrf, "X-Step-Up-Token": step})
        assert r.status_code == 200 and r.json()["revoked_at"] is not None
        assert authz.is_revoked(minted["key_id"])

        # Token and audit never leak the secret.
        async with async_session() as s:
            events = (await s.execute(select(AuditEvent))).scalars().all()
        assert {"operator.login", "key.mint", "key.revoke"} <= {e.action for e in events}
        assert minted["token"].split(".")[1] not in str([e.detail for e in events])


async def test_mint_key_validates_expiry(db, pepper_file, authz):
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        assert (await c.post("/api/keys", json={"expires_at": soon}, headers=csrf)).status_code == 201
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert (await c.post("/api/keys", json={"expires_at": past}, headers=csrf)).status_code == 422
        assert (await c.post("/api/keys", json={"bogus": 1}, headers=csrf)).status_code == 422


async def test_revoke_unknown_key_404(db, pepper_file, authz):
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        step = (
            await c.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf)
        ).json()["token"]
        r = await c.post("/api/keys/nope/revoke", headers={**csrf, "X-Step-Up-Token": step})
        assert r.status_code == 404


async def test_grant_and_ungrant(db, pepper_file, authz):
    async with async_session() as s:
        server = Server(name="s", slug="s", kind="stdio", command="npx", args=[])
        s.add(server)
        await s.commit()
    async with _client(_auth()) as c:
        csrf = await _logged_in(c)
        key_id = (await c.post("/api/keys", json={}, headers=csrf)).json()["key_id"]
        g = f"/api/keys/{key_id}/grants/{server.id}"
        assert (await c.put(g, headers=csrf)).status_code == 200
        assert (await c.put(g, headers=csrf)).status_code == 200  # idempotent
        assert authz.has_grant(key_id, server.id)
        assert (await c.get("/api/keys")).json()[0]["server_ids"] == [str(server.id)]
        assert (await c.put(f"/api/keys/{key_id}/grants/{uuid.uuid4()}", headers=csrf)).status_code == 404

        # Ungrant needs step-up.
        assert (await c.delete(g, headers=csrf)).status_code == 403
        step = (
            await c.post("/api/auth/step-up", json={"password": PASSWORD}, headers=csrf)
        ).json()["token"]
        assert (await c.delete(g, headers={**csrf, "X-Step-Up-Token": step})).status_code == 200
        assert not authz.has_grant(key_id, server.id)
    async with async_session() as s:
        assert (await s.execute(select(Grant))).first() is None
        assert (await s.execute(select(AgentKey))).scalar_one().key_id == key_id
