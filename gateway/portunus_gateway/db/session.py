"""Async engine/session factory. URL comes from ``PORTUNUS_DATABASE_URL``."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from portunus_gateway.db.url import database_url


def _make_engine(url: str):
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    if is_sqlite and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    # SQLite connections are cheap; NullPool avoids cross-event-loop connection reuse.
    eng = create_async_engine(url, poolclass=NullPool) if is_sqlite else create_async_engine(url)
    if is_sqlite:

        @event.listens_for(eng.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return eng


engine = _make_engine(database_url())
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
