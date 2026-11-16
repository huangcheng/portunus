"""Async engine/session factory. URL comes from ``PORTUNUS_DATABASE_URL``."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from portunus_gateway.db.url import database_url, ensure_sqlite_dir


def _sqlite_memory(parsed) -> bool:
    """In-memory forms ``ensure_sqlite_dir`` already treats as non-files."""
    return parsed.database in (None, "", ":memory:")


def _make_engine(url: str):
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    ensure_sqlite_dir(url)
    if is_sqlite and _sqlite_memory(parsed):
        # NullPool opens a new connection per checkout, and each ``:memory:``
        # connection is a private empty database — schema from create_all would
        # be invisible to the next session. One shared connection is required.
        eng = create_async_engine(
            url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    elif is_sqlite:
        # File-backed connections are cheap; NullPool avoids cross-event-loop reuse.
        eng = create_async_engine(url, poolclass=NullPool)
    else:
        eng = create_async_engine(url)
    if is_sqlite:

        @event.listens_for(eng.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return eng


engine = _make_engine(database_url())
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
