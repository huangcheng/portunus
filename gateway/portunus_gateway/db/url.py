"""Database URL resolution (import-light: no engine is created here)."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import make_url

DATABASE_URL_ENV = "PORTUNUS_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/portunus.db"


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL


def ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory of a file-backed SQLite database (no-op otherwise)."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
