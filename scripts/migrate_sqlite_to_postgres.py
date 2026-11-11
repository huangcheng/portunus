#!/usr/bin/env python3
"""One-shot SQLite → Postgres copy for Portunus (preserves PKs and LargeBinary).

Usage (from gateway venv, with both DBs reachable):

  python scripts/migrate_sqlite_to_postgres.py \\
    --sqlite sqlite:////var/lib/portunus/portunus.db \\
    --postgres postgresql+psycopg://portunus:SECRET@postgres:5432/portunus

Does not log secret material. Fails if destination tables are non-empty or counts mismatch.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url

# FK-safe order. alembic_version is created by upgrade head — not copied from SQLite.
TABLES = (
    "servers",
    "tools",
    "vault_entries",
    "catalog_apps",
    "agent_keys",
    "grants",
    "operator_sessions",
    "audit_events",
)


def _sync_url(url: str) -> str:
    """Normalize async URLs to sync drivers for this script."""
    u = make_url(url)
    driver = u.get_driver_name()
    if driver == "aiosqlite":
        return url.replace("sqlite+aiosqlite:", "sqlite:", 1)
    if driver == "asyncpg":
        return url.replace("postgresql+asyncpg:", "postgresql+psycopg:", 1)
    return url


def _gateway_root() -> Path:
    """Resolve gateway dir (repo `gateway/` or image `/app/gateway`)."""
    env = Path(os.environ["PORTUNUS_GATEWAY_ROOT"]) if os.environ.get("PORTUNUS_GATEWAY_ROOT") else None
    candidates = [
        env,
        Path("/app/gateway"),
        Path(__file__).resolve().parents[1] / "gateway",
        Path.cwd(),
    ]
    for c in candidates:
        if c is None:
            continue
        if (c / "alembic.ini").is_file() and (c / "portunus_gateway").is_dir():
            return c
    raise SystemExit("cannot find gateway root (set PORTUNUS_GATEWAY_ROOT)")


def _alembic_upgrade(postgres_url: str) -> None:
    gateway = _gateway_root()
    ini = gateway / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", _sync_url(postgres_url))
    if str(gateway) not in sys.path:
        sys.path.insert(0, str(gateway))
    command.upgrade(cfg, "head")


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one())


def _assert_empty(engine: Engine) -> None:
    for name in TABLES:
        n = _count(engine, name)
        if n != 0:
            raise SystemExit(f"destination table {name!r} is not empty ({n} rows); abort")


def migrate(sqlite_url: str, postgres_url: str) -> None:
    src_url = _sync_url(sqlite_url)
    dst_url = _sync_url(postgres_url)
    if make_url(src_url).get_backend_name() != "sqlite":
        raise SystemExit("source must be sqlite")
    if make_url(dst_url).get_backend_name() != "postgresql":
        raise SystemExit("destination must be postgresql")

    print("alembic upgrade head on destination…", flush=True)
    _alembic_upgrade(dst_url)

    src = create_engine(src_url)
    dst = create_engine(dst_url)
    try:
        _assert_empty(dst)

        src_meta = MetaData()
        src_meta.reflect(bind=src, only=list(TABLES))
        dst_meta = MetaData()
        dst_meta.reflect(bind=dst, only=list(TABLES))

        missing_src = [t for t in TABLES if t not in src_meta.tables]
        if missing_src:
            raise SystemExit(f"source missing tables: {missing_src}")

        counts: dict[str, int] = {}
        with src.connect() as sc, dst.begin() as dc:
            for name in TABLES:
                stable = src_meta.tables[name]
                dtable = dst_meta.tables[name]
                rows = list(sc.execute(select(stable)).mappings())
                counts[name] = len(rows)
                if not rows:
                    print(f"  {name}: 0 rows", flush=True)
                    continue
                # Insert in chunks; preserve column names/values (incl. LargeBinary).
                payload = [dict(r) for r in rows]
                dc.execute(dtable.insert(), payload)
                print(f"  {name}: {len(payload)} rows", flush=True)

        for name, expected in counts.items():
            got = _count(dst, name)
            if got != expected:
                raise SystemExit(f"count mismatch for {name}: src={expected} dst={got}")

        with dst.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        print(f"ok alembic_version={ver} tables={counts}", flush=True)
    finally:
        src.dispose()
        dst.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sqlite", required=True, help="SQLite SQLAlchemy URL")
    p.add_argument("--postgres", required=True, help="Postgres SQLAlchemy URL")
    args = p.parse_args()
    migrate(args.sqlite, args.postgres)


if __name__ == "__main__":
    main()
