"""Encrypt operator TOTP secrets and hash recovery codes.

The Fernet key is drawn from ``PORTUNUS_SECRETS_KEY`` (``kN:`` entries). New
material is sealed/hashed under the newest key; decryption and recovery-code
verification try every valid key version (newest first) so rotation does not
invalidate outstanding credentials. Recovery codes use HMAC-SHA256 over the
key with a domain-separated prefix, so a database leak is not an offline
brute-force of the short codes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re

from cryptography.fernet import Fernet, InvalidToken

from .vault import KEY_LEN

SECRETS_KEY_ENV = "PORTUNUS_SECRETS_KEY"
_ENTRY_RE = re.compile(r"^k([1-9][0-9]*):(.+)$")
_RECOVERY_DOMAIN = b"portunus-recovery-v1:"


class OperatorCryptoError(Exception):
    """Keyring missing or a ciphertext that will not open. No key material."""


def _parse_keys(keyring: str | None = None) -> list[tuple[int, bytes]]:
    """Return ``(version, key)`` pairs sorted newest-first."""
    raw = os.environ.get(SECRETS_KEY_ENV, "") if keyring is None else keyring
    found: dict[int, bytes] = {}
    for part in raw.split(","):
        match = _ENTRY_RE.match(part.strip())
        if not match:
            continue
        version = int(match.group(1))
        try:
            key = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise OperatorCryptoError(
                f"{SECRETS_KEY_ENV} entry k{version} is not valid base64"
            ) from None
        if len(key) != KEY_LEN:
            raise OperatorCryptoError(
                f"{SECRETS_KEY_ENV} entry k{version} must decode to {KEY_LEN} bytes"
            )
        found[version] = key
    if not found:
        raise OperatorCryptoError(f"{SECRETS_KEY_ENV} has no valid 32-byte entry")
    return sorted(found.items(), key=lambda item: item[0], reverse=True)


def active_key_bytes(keyring: str | None = None) -> bytes:
    """Raw 32-byte key from the highest valid ``kN`` entry."""
    return _parse_keys(keyring)[0][1]


def all_key_bytes(keyring: str | None = None) -> list[bytes]:
    """Every valid key, newest first."""
    return [key for _version, key in _parse_keys(keyring)]


def _fernet(key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_totp_secret(secret: str) -> str:
    return _fernet(active_key_bytes()).encrypt(secret.encode()).decode()


def decrypt_totp_secret(token: str) -> str:
    last: Exception | None = None
    for key in all_key_bytes():
        try:
            return _fernet(key).decrypt(token.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            last = exc
    raise OperatorCryptoError("totp secret could not be decrypted") from last


def hash_recovery_code(normalized: str, *, key: bytes | None = None) -> str:
    material = active_key_bytes() if key is None else key
    return hmac.new(material, _RECOVERY_DOMAIN + normalized.encode(), hashlib.sha256).hexdigest()


def recovery_code_digests(normalized: str) -> list[str]:
    """Hashes under every key version (newest first) for rotation-safe verify."""
    return [hash_recovery_code(normalized, key=key) for key in all_key_bytes()]
