import base64
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from portunus_gateway import config
from portunus_gateway.config import PEPPER_FILE_ENV, load_pepper
from portunus_gateway.db.models import AgentKey
from portunus_gateway.db.session import async_session
from portunus_gateway.keys import AuthError, mint_agent_key, revoke_key, verify_agent_token
from portunus_gateway.vault import VaultError
from portunus_shared import PortunusError

pytestmark = pytest.mark.anyio

B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


async def test_mint_and_verify(pepper_file, db):
    token, row = await mint_agent_key(expires_at=None)
    assert token.startswith("pm-")
    parts = token[3:].split(".")
    assert len(parts[0]) == 32 and len(parts[1]) == 43
    assert all(B64URL.match(p) for p in parts)
    assert (await verify_agent_token(token)).key_id == row.key_id


async def test_only_digest_is_stored_without_vault(pepper_file, db):
    token, row = await mint_agent_key(expires_at=None)
    secret = token.split(".")[1]
    async with async_session() as s:
        stored = (await s.execute(select(AgentKey))).scalar_one()
    assert len(stored.digest) == 32
    assert secret.encode() not in stored.digest and stored.digest != base64.urlsafe_b64decode(secret + "=")
    assert row.key_id == stored.key_id
    assert stored.secret_ciphertext is None


async def test_vault_seals_recoverable_secret(pepper_file, db):
    import os

    from portunus_gateway.keys import reveal_agent_token
    from portunus_gateway.vault import Vault

    vault = Vault({1: os.urandom(32)})
    token, row = await mint_agent_key(expires_at=None, vault=vault)
    assert row.secret_ciphertext and row.secret_key_version == 1
    assert token.encode() not in row.secret_ciphertext
    assert await reveal_agent_token(row.key_id, vault) == token


async def test_tokens_are_unique(pepper_file, db):
    (t1, r1), (t2, r2) = await mint_agent_key(expires_at=None), await mint_agent_key(expires_at=None)
    assert t1 != t2 and r1.key_id != r2.key_id


async def test_label_is_stored(pepper_file, db):
    _, row = await mint_agent_key(expires_at=None, label="ci-bot")
    assert row.label == "ci-bot"


async def test_revoke_deletes_row_and_fails_verify(pepper_file, db, authz):
    token, row = await mint_agent_key(expires_at=None)
    await revoke_key(row.key_id)
    async with async_session() as s:
        assert (await s.execute(select(AgentKey))).scalar_one_or_none() is None
    assert authz.is_revoked(row.key_id)
    with pytest.raises(AuthError) as e:
        await verify_agent_token(token)
    assert e.value.code == "unauthorized"


async def test_revoke_second_time_raises(pepper_file, db, authz):
    _, row = await mint_agent_key(expires_at=None)
    await revoke_key(row.key_id)
    with pytest.raises(AuthError) as e:
        await revoke_key(row.key_id)
    assert e.value.code == "unauthorized"


async def test_revoke_unknown_key_raises(pepper_file, db):
    with pytest.raises(AuthError) as e:
        await revoke_key("nope")
    assert e.value.code == "unauthorized"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "garbage",
        "pm-",
        "pm-abc",
        "pm-abc.",
        "pm-.abc",
        "Bearer pm-x.y",
        "xx-" + "A" * 32 + "." + "B" * 43,
        "pm-" + "A" * 31 + "." + "B" * 43,
        "pm-" + "A" * 32 + "." + "B" * 42,
        "pm-" + "A" * 32 + "." + "B" * 44,
        "pm-" + "A" * 32 + "." + "B" * 42 + "!",
        "pm-" + "A" * 32 + "." + "B" * 43 + ".extra",
    ],
)
async def test_malformed_tokens_are_unauthorized(pepper_file, db, bad):
    with pytest.raises(AuthError) as e:
        await verify_agent_token(bad)
    assert e.value.code == "unauthorized"


async def test_unknown_key_id_is_unauthorized(pepper_file, db):
    with pytest.raises(AuthError) as e:
        await verify_agent_token("pm-" + "A" * 32 + "." + "B" * 43)
    assert e.value.code == "unauthorized"


async def test_wrong_secret_is_unauthorized(pepper_file, db):
    token, _ = await mint_agent_key(expires_at=None)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(AuthError) as e:
        await verify_agent_token(forged)
    assert e.value.code == "unauthorized"


async def test_wrong_secret_on_deleted_key_is_unauthorized(pepper_file, db, authz):
    token, row = await mint_agent_key(expires_at=None)
    await revoke_key(row.key_id)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(AuthError) as e:
        await verify_agent_token(forged)
    assert e.value.code == "unauthorized"


async def test_pepper_rotation_invalidates_keys(pepper_file, db, tmp_path, monkeypatch):
    token, _ = await mint_agent_key(expires_at=None)
    other = tmp_path / "pepper2"
    other.write_text(base64.b64encode(b"\x01" * 32).decode())
    monkeypatch.setenv(PEPPER_FILE_ENV, str(other))
    config.clear_pepper_cache()
    with pytest.raises(AuthError) as e:
        await verify_agent_token(token)
    assert e.value.code == "unauthorized"


async def test_expired_key_is_unauthorized(pepper_file, db):
    token, _ = await mint_agent_key(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(AuthError) as e:
        await verify_agent_token(token)
    assert e.value.code == "unauthorized"


async def test_future_expiry_verifies(pepper_file, db):
    token, row = await mint_agent_key(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert (await verify_agent_token(token)).key_id == row.key_id


async def test_naive_expires_at_is_treated_as_utc(pepper_file, db):
    """SQLite returns naive datetimes on read; naive input/output must be normalised to UTC."""
    naive_future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    token, row = await mint_agent_key(expires_at=naive_future)
    assert row.expires_at.tzinfo is not None
    assert await verify_agent_token(token)
    naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    token2, _ = await mint_agent_key(expires_at=naive_past)
    with pytest.raises(AuthError):
        await verify_agent_token(token2)


async def test_expiry_read_back_naive_from_sqlite_still_compares(pepper_file, db):
    token, row = await mint_agent_key(expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    async with async_session() as s:
        stored = (await s.execute(select(AgentKey).where(AgentKey.key_id == row.key_id))).scalar_one()
    # (SQLite DateTime(timezone=True) round-trips naive; either way verify must not raise TypeError.)
    with pytest.raises(AuthError):
        await verify_agent_token(token)
    assert stored.expires_at is not None


async def test_deleted_key_is_unauthorized_even_if_expired(pepper_file, db, authz):
    token, row = await mint_agent_key(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    await revoke_key(row.key_id)
    with pytest.raises(AuthError) as e:
        await verify_agent_token(token)
    assert e.value.code == "unauthorized"


def test_auth_error_is_portunus_error():
    assert issubclass(AuthError, PortunusError)
    with pytest.raises(ValueError):
        AuthError("not_a_code")


def test_missing_pepper_env_raises(monkeypatch):
    monkeypatch.delenv(PEPPER_FILE_ENV, raising=False)
    config.clear_pepper_cache()
    with pytest.raises(VaultError):
        load_pepper()


@pytest.mark.parametrize("content", ["", "   \n", "not base64!!", base64.b64encode(b"short").decode()])
def test_bad_pepper_file_raises(tmp_path, monkeypatch, content):
    p = tmp_path / "pepper"
    p.write_text(content)
    monkeypatch.setenv(PEPPER_FILE_ENV, str(p))
    config.clear_pepper_cache()
    with pytest.raises(VaultError) as e:
        load_pepper()
    assert content.strip() not in str(e.value) or not content.strip()


def test_missing_pepper_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(PEPPER_FILE_ENV, str(tmp_path / "absent"))
    config.clear_pepper_cache()
    with pytest.raises(VaultError):
        load_pepper()


def test_good_pepper_loads(pepper_file):
    assert len(load_pepper()) >= 32
