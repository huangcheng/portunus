"""Gateway configuration helpers."""

from __future__ import annotations

import os

from portunus_gateway.vault import VaultError

SECRETS_KEY_ENV = "PORTUNUS_SECRETS_KEY"


def load_keyring() -> str:
    """Return the raw keyring string (``k1:<base64-32-bytes>[,k2:...]``) from env."""
    value = os.environ.get(SECRETS_KEY_ENV)
    if not value:
        raise VaultError(f"{SECRETS_KEY_ENV} is not set")
    return value
