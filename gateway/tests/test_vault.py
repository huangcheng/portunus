import base64
import os

import pytest

from portunus_gateway.config import SECRETS_KEY_ENV, load_keyring
from portunus_gateway.vault import Vault, VaultError


def _keyring(version: int = 1) -> str:
    return f"k{version}:{base64.b64encode(os.urandom(32)).decode()}"


def test_seal_open_roundtrip():
    key = base64.b64encode(os.urandom(32)).decode()
    v = Vault.from_keyring(f"k1:{key}")
    pt = b"sk-secret"
    ct = v.seal(pt, vault_entry_id="e1", key_version=1)
    assert ct != pt
    assert v.open(ct, vault_entry_id="e1", key_version=1) == pt


def test_aad_mismatch_fails():
    v = Vault.from_keyring(_keyring())
    ct = v.seal(b"x", vault_entry_id="e1", key_version=1)
    with pytest.raises(VaultError):
        v.open(ct, vault_entry_id="e2", key_version=1)


def test_nonce_is_random_per_seal():
    v = Vault.from_keyring(_keyring())
    a = v.seal(b"same", vault_entry_id="e1", key_version=1)
    b = v.seal(b"same", vault_entry_id="e1", key_version=1)
    assert a != b


def test_tampered_blob_fails():
    v = Vault.from_keyring(_keyring())
    ct = bytearray(v.seal(b"payload", vault_entry_id="e1", key_version=1))
    ct[-1] ^= 0x01
    with pytest.raises(VaultError):
        v.open(bytes(ct), vault_entry_id="e1", key_version=1)


def test_short_blob_fails():
    v = Vault.from_keyring(_keyring())
    with pytest.raises(VaultError):
        v.open(b"short", vault_entry_id="e1", key_version=1)


def test_wrong_key_fails():
    ct = Vault.from_keyring(_keyring()).seal(b"x", vault_entry_id="e1", key_version=1)
    with pytest.raises(VaultError):
        Vault.from_keyring(_keyring()).open(ct, vault_entry_id="e1", key_version=1)


def test_key_version_is_bound_and_must_exist():
    ring = f"{_keyring(1)},{_keyring(2)}"
    v = Vault.from_keyring(ring)
    ct = v.seal(b"x", vault_entry_id="e1", key_version=2)
    assert v.open(ct, vault_entry_id="e1", key_version=2) == b"x"
    with pytest.raises(VaultError):
        v.open(ct, vault_entry_id="e1", key_version=1)
    with pytest.raises(VaultError):
        v.seal(b"x", vault_entry_id="e1", key_version=3)
    with pytest.raises(VaultError):
        v.open(ct, vault_entry_id="e1", key_version=3)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "garbage",
        "k1:",
        "k1:not-base64!!",
        f"k1:{base64.b64encode(os.urandom(16)).decode()}",
        f"x1:{base64.b64encode(os.urandom(32)).decode()}",
        f"k0:{base64.b64encode(os.urandom(32)).decode()}",
        "k1:" + base64.b64encode(os.urandom(32)).decode() + ",k1:" + base64.b64encode(os.urandom(32)).decode(),
    ],
)
def test_bad_keyring_rejected(bad):
    with pytest.raises(VaultError):
        Vault.from_keyring(bad)


def test_error_does_not_leak_key_material():
    key = base64.b64encode(os.urandom(16)).decode()
    with pytest.raises(VaultError) as ei:
        Vault.from_keyring(f"k1:{key}")
    assert key not in str(ei.value)


def test_load_keyring_from_env(monkeypatch):
    ring = _keyring()
    monkeypatch.setenv(SECRETS_KEY_ENV, ring)
    assert SECRETS_KEY_ENV == "PORTUNUS_SECRETS_KEY"
    assert load_keyring() == ring


def test_load_keyring_missing_env(monkeypatch):
    monkeypatch.delenv(SECRETS_KEY_ENV, raising=False)
    with pytest.raises(VaultError):
        load_keyring()
