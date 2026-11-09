"""Database URL resolution (import-light: no engine is created here)."""

from __future__ import annotations

import os

DATABASE_URL_ENV = "PORTUNUS_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/portunus.db"


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL
