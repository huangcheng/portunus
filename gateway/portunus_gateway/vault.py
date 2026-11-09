"""AES-256-GCM vault with AAD binding to (vault_entry_id, key_version).

Blob layout: ``nonce (12 bytes) || ciphertext+tag``.
Keyring: comma-separated ``k<version>:<base64-32-bytes>`` entries.
"""

from __future__ import annotations

import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12
KEY_LEN = 32
_ENTRY_RE = re.compile(r"^k([1-9][0-9]*):(.+)$")


class VaultError(Exception):
    """Vault failure. Messages never contain key or plaintext material."""


def _aad(vault_entry_id: str, key_version: int) -> bytes:
    return f"{vault_entry_id}|{key_version}".encode()


class Vault:
    def __init__(self, keys: dict[int, bytes]) -> None:
        if not keys:
            raise VaultError("keyring is empty")
        self._aeads = {v: AESGCM(k) for v, k in keys.items()}

    @classmethod
    def from_keyring(cls, keyring: str) -> Vault:
        if not keyring or not keyring.strip():
            raise VaultError("keyring is empty")
        keys: dict[int, bytes] = {}
        for raw in keyring.split(","):
            m = _ENTRY_RE.match(raw.strip())
            if not m:
                raise VaultError("malformed keyring entry (expected k<n>:<base64-32-bytes>)")
            version = int(m.group(1))
            if version in keys:
                raise VaultError(f"duplicate key version k{version}")
            try:
                key = base64.b64decode(m.group(2), validate=True)
            except (binascii.Error, ValueError):
                raise VaultError(f"key k{version} is not valid base64") from None
            if len(key) != KEY_LEN:
                raise VaultError(f"key k{version} must decode to {KEY_LEN} bytes")
            keys[version] = key
        return cls(keys)

    def _aead(self, key_version: int) -> AESGCM:
        try:
            return self._aeads[key_version]
        except KeyError:
            raise VaultError(f"unknown key version {key_version}") from None

    def seal(self, plaintext: bytes, *, vault_entry_id: str, key_version: int) -> bytes:
        aead = self._aead(key_version)
        nonce = os.urandom(NONCE_LEN)
        return nonce + aead.encrypt(nonce, plaintext, _aad(vault_entry_id, key_version))

    def open(self, blob: bytes, *, vault_entry_id: str, key_version: int) -> bytes:
        aead = self._aead(key_version)
        if len(blob) < NONCE_LEN + 16:
            raise VaultError("ciphertext too short")
        nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
        try:
            return aead.decrypt(nonce, ct, _aad(vault_entry_id, key_version))
        except InvalidTag:
            raise VaultError("decryption failed (wrong key, AAD, or tampered data)") from None
