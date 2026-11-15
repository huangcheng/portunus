"""Encrypt operator TOTP secrets and hash recovery codes.

The Fernet key is the active vault key (newest ``kN:`` entry in
``PORTUNUS_SECRETS_KEY``), urlsafe-base64 encoded. Recovery codes use
HMAC-SHA256 over that same key with a domain-separated prefix, so a
database leak is not an offline brute-force of the short codes.
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


def active_key_bytes(keyring: str | None = None) -> bytes:
    """Raw 32-byte key from the highest valid ``kN`` entry."""
    raw = os.environ.get(SECRETS_KEY_ENV, "") if keyring is None else keyring
    best_version = -1
    best: bytes | None = None
    for part in raw.split(","):
        match = _ENTRY_RE.match(part.strip())
        if not match:
            continue
        version = int(match.group(1))
        try:
            key = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(key) == KEY_LEN and version > best_version:
            best_version = version
            best = key
    if best is None:
        raise OperatorCryptoError(f"{SECRETS_KEY_ENV} has no valid 32-byte entry")
    return best


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(active_key_bytes()))


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_totp_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise OperatorCryptoError("totp secret could not be decrypted") from exc


def hash_recovery_code(normalized: str) -> str:
    return hmac.new(active_key_bytes(), _RECOVERY_DOMAIN + normalized.encode(), hashlib.sha256).hexdigest()
