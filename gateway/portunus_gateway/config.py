"""Gateway configuration helpers."""

from __future__ import annotations

import base64
import binascii
import os

from portunus_gateway.vault import VaultError

SECRETS_KEY_ENV = "PORTUNUS_SECRETS_KEY"


def load_keyring() -> str:
    """Return the raw keyring string (``k1:<base64-32-bytes>[,k2:...]``) from env."""
    value = os.environ.get(SECRETS_KEY_ENV)
    if not value:
        raise VaultError(f"{SECRETS_KEY_ENV} is not set")
    return value


PEPPER_FILE_ENV = "PORTUNUS_PEPPER_FILE"
MIN_PEPPER_LEN = 32

_pepper_cache: dict[str, bytes] = {}


def clear_pepper_cache() -> None:
    _pepper_cache.clear()


def load_pepper() -> bytes:
    """Return the agent-key pepper (>= 32 bytes) from the file named by ``PORTUNUS_PEPPER_FILE``.

    The file holds the pepper base64-encoded (like the vault keyring). It is read once per path
    and cached for the process lifetime; rotating the pepper requires a restart (and invalidates
    every issued agent key). Error messages never include file contents.
    """
    path = os.environ.get(PEPPER_FILE_ENV)
    if not path:
        raise VaultError(f"{PEPPER_FILE_ENV} is not set")
    cached = _pepper_cache.get(path)
    if cached is not None:
        return cached
    try:
        with open(path, "rb") as fh:
            raw = fh.read().strip()
    except OSError as exc:
        raise VaultError(f"cannot read pepper file: {type(exc).__name__}") from None
    try:
        pepper = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise VaultError("pepper file is not valid base64") from None
    if len(pepper) < MIN_PEPPER_LEN:
        raise VaultError(f"pepper must decode to at least {MIN_PEPPER_LEN} bytes")
    _pepper_cache[path] = pepper
    return pepper
