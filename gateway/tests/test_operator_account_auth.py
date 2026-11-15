"""Operator account: one-time setup, password login, TOTP challenge, Turnstile, lockout."""

from __future__ import annotations

import base64
import contextlib
import os

import httpx
import pyotp
import pytest
from argon2 import PasswordHasher
from portunus_gateway.db.session import async_session
from portunus_gateway.operator_api import create_app
from portunus_gateway.operator_auth import OperatorAuth
from portunus_gateway.registry import Registry
from portunus_gateway.vault import Vault

pytestmark = pytest.mark.anyio

ACCOUNT_PASSWORD = "account-password-ok"
ENV_PASSWORD = "env-bootstrap-secret"
_FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
_SECRETS = "k1:" + base64.b64encode(b"k" * 32).decode()


def _registry() -> Registry:
    return Registry(
        async_session,
        Vault({1: os.urandom(32)}),
        object(),
        trusted_path=[],
        binary_allowlist=[],
    )


def _auth(**kw) -> OperatorAuth:
    kw.setdefault("secure_cookie", False)
    return OperatorAuth.from_password(
        ENV_PASSWORD, session_factory=async_session, hasher=_FAST, **kw
    )


def _auth_no_env(**kw) -> OperatorAuth:
    kw.setdefault("secure_cookie", False)
    return OperatorAuth(None, session_factory=async_session, hasher=_FAST, **kw)


@contextlib.asynccontextmanager
async def _client(auth: OperatorAuth, *, ip: str = "10.1.0.8"):
    app = create_app(_registry(), auth=auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://test"
    ) as c:
        yield c


async def _setup(c: httpx.AsyncClient, *, bootstrap: str | None = None, username: str = "ada") -> httpx.Response:
    body: dict[str, str] = {"username": username, "password": ACCOUNT_PASSWORD}
    if bootstrap is not None:
        body["bootstrap_password"] = bootstrap
    return await c.post("/api/auth/setup", json=body)


# ---- setup once -----------------------------------------------------------------------


async def test_setup_is_once_and_logs_in(db, monkeypatch):
    monkeypatch.delenv("PORTUNUS_OPERATOR_PASSWORD", raising=False)
    async with _client(_auth_no_env()) as c:
        status = (await c.get("/api/auth/status")).json()
        assert status["configured"] is False
        assert status["bootstrap_required"] is False
        first = await _setup(c)
        assert first.status_code == 200, first.text
        assert first.json()["username"] == "ada"
        assert "csrf_token" in first.json()
        assert first.headers["set-cookie"].lower().startswith("portunus_session=")
        again = await _setup(c, username="ada")
        assert again.status_code == 409
        assert again.json()["code"] == "already_configured"
        assert (await c.get("/api/auth/status")).json()["configured"] is True
        assert (await c.get("/api/auth/session")).json()["username"] == "ada"


async def test_setup_requires_bootstrap_when_env_password_set(db, monkeypatch):
    monkeypatch.setenv("PORTUNUS_OPERATOR_PASSWORD", ENV_PASSWORD)
    async with _client(_auth()) as c:
        assert (await c.get("/api/auth/status")).json()["bootstrap_required"] is True
        missing = await _setup(c)
        assert missing.status_code == 401
        wrong = await _setup(c, bootstrap="not-the-env-password")
        assert wrong.status_code == 401
        ok = await _setup(c, bootstrap=ENV_PASSWORD)
        assert ok.status_code == 200, ok.text
        # Configured account rejects the env password by itself.
        alone = await c.post("/api/auth/login", json={"password": ENV_PASSWORD})
        assert alone.status_code == 401
        assert alone.json()["code"] == "invalid_credentials"
        good = await c.post(
            "/api/auth/login", json={"username": "ada", "password": ACCOUNT_PASSWORD}
        )
        assert good.status_code == 200, good.text
        assert good.json()["username"] == "ada"


# ---- TOTP challenge -------------------------------------------------------------------


async def test_login_issues_totp_challenge_then_accepts_code(db, monkeypatch):
    monkeypatch.delenv("PORTUNUS_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setenv("PORTUNUS_SECRETS_KEY", _SECRETS)
    async with _client(_auth_no_env()) as c:
        assert (await _setup(c)).status_code == 200
        csrf = {"X-CSRF-Token": (await c.get("/api/auth/session")).json()["csrf_token"]}
        enabled = await c.post("/api/auth/totp/enable", headers=csrf)
        assert enabled.status_code == 200, enabled.text
        secret = enabled.json()["secret"]
        code = pyotp.TOTP(secret).now()
        confirmed = await c.post("/api/auth/totp/confirm", json={"secret": secret, "code": code}, headers=csrf)
        assert confirmed.status_code == 200, confirmed.text
        assert len(confirmed.json()["codes"]) == 10
        recovery = confirmed.json()["codes"][0]
        changed = await c.post(
            "/api/auth/password",
            json={"current": ACCOUNT_PASSWORD, "next": "account-password-ok2"},
            headers=csrf,
        )
        assert changed.status_code == 200, changed.text
        step_up = await c.post("/api/auth/step-up", json={"password": "account-password-ok2"}, headers=csrf)
        assert step_up.status_code == 200, step_up.text
        assert step_up.json()["step_up_token"]
        assert (await c.post("/api/auth/logout", headers=csrf)).status_code == 204

        step = await c.post("/api/auth/login", json={"username": "ada", "password": "account-password-ok2"})
        assert step.status_code == 401
        body = step.json()
        assert body["code"] == "totp_required"
        assert body["challenge_token"]
        assert "set-cookie" not in step.headers

        bad = await c.post(
            "/api/auth/login/totp",
            json={"challenge_token": body["challenge_token"], "code": "000000"},
        )
        assert bad.status_code == 401
        assert bad.json()["code"] == "invalid_code"

        ok = await c.post(
            "/api/auth/login/totp",
            json={"challenge_token": body["challenge_token"], "code": pyotp.TOTP(secret).now()},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["csrf_token"]
        assert (await c.get("/api/servers")).status_code == 200
        csrf2 = {"X-CSRF-Token": ok.json()["csrf_token"]}
        assert (await c.post("/api/auth/logout", headers=csrf2)).status_code == 204
        again = await c.post(
            "/api/auth/login", json={"username": "ada", "password": "account-password-ok2"}
        )
        assert again.status_code == 401 and again.json()["code"] == "totp_required"
        used = await c.post(
            "/api/auth/login/totp",
            json={"challenge_token": again.json()["challenge_token"], "code": recovery},
        )
        assert used.status_code == 200, used.text
        assert used.json()["recovery_remaining"] == 9


# ---- Turnstile ------------------------------------------------------------------------


async def test_turnstile_blocks_login_until_siteverify_passes(db, monkeypatch):
    monkeypatch.delenv("PORTUNUS_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setenv("PORTUNUS_TURNSTILE_SECRET", "test-secret")
    monkeypatch.setenv("PORTUNUS_TURNSTILE_HOSTNAMES", "test")
    monkeypatch.setenv("PORTUNUS_TURNSTILE_SITEKEY", "0x4AAAAAAGBsF0uydauQCt2F")

    async def _verify(token: str, remoteip: str | None = None) -> bool:
        return token == "human-ok"

    monkeypatch.setattr("portunus_gateway.operator_api.verify_login_token", _verify)
    async with _client(_auth_no_env()) as c:
        status = (await c.get("/api/auth/status")).json()
        assert status["turnstile_sitekey"] == "0x4AAAAAAGBsF0uydauQCt2F"
        setup = await _setup(c)
        assert setup.status_code == 200
        csrf = {"X-CSRF-Token": setup.json()["csrf_token"]}
        assert (await c.post("/api/auth/logout", headers=csrf)).status_code == 204

        missing = await c.post("/api/auth/login", json={"username": "ada", "password": ACCOUNT_PASSWORD})
        assert missing.status_code == 403
        assert missing.json()["code"] == "turnstile_failed"
        rejected = await c.post(
            "/api/auth/login",
            json={"username": "ada", "password": ACCOUNT_PASSWORD, "turnstile_token": "bot"},
        )
        assert rejected.status_code == 403
        # A failed human check does not burn the password lockout.
        ok = await c.post(
            "/api/auth/login",
            json={"username": "ada", "password": ACCOUNT_PASSWORD, "turnstile_token": "human-ok"},
        )
        assert ok.status_code == 200, ok.text


# ---- lockout --------------------------------------------------------------------------


async def test_five_bad_account_passwords_lock_the_ip(db, monkeypatch):
    monkeypatch.delenv("PORTUNUS_OPERATOR_PASSWORD", raising=False)
    async with _client(_auth_no_env()) as c:
        setup = await _setup(c)
        assert setup.status_code == 200
        csrf = {"X-CSRF-Token": setup.json()["csrf_token"]}
        assert (await c.post("/api/auth/logout", headers=csrf)).status_code == 204
        for _ in range(5):
            r = await c.post("/api/auth/login", json={"username": "ada", "password": "wrong-password"})
            assert r.status_code == 401
            assert r.json()["code"] == "invalid_credentials"
        locked = await c.post(
            "/api/auth/login", json={"username": "ada", "password": ACCOUNT_PASSWORD}
        )
        assert locked.status_code == 429
        assert locked.json()["code"] == "locked_out"
        assert int(locked.headers["retry-after"]) > 0
