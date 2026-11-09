"""Alembic environment. Supports async (aiosqlite/asyncpg) and sync URLs."""

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from portunus_gateway.db.models import Base
from portunus_gateway.db.url import database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
ASYNC_DRIVERS = {"aiosqlite", "asyncpg"}


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or database_url()


def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=make_url(_url()).get_backend_name() == "sqlite",
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


def run_migrations_online() -> None:
    url = _url()
    if make_url(url).get_driver_name() in ASYNC_DRIVERS:
        asyncio.run(_run_async(url))
        return
    from sqlalchemy import create_engine

    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
