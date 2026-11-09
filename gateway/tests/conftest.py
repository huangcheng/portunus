import os
import tempfile

import pytest

# Isolated SQLite file for the whole test run; must be set before the db package is imported.
_TMP_DIR = tempfile.mkdtemp(prefix="portunus-test-")
os.environ["PORTUNUS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DIR}/portunus.db"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_db():
    """Drop all tables after each test so no state leaks between tests."""
    yield
    from sqlalchemy import create_engine

    from portunus_gateway.db.models import Base

    eng = create_engine(os.environ["PORTUNUS_DATABASE_URL"].replace("+aiosqlite", ""))
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def pepper_file(tmp_path, monkeypatch):
    """Point ``PORTUNUS_PEPPER_FILE`` at a fresh random pepper (never committed)."""
    import base64
    import secrets

    from portunus_gateway.config import PEPPER_FILE_ENV, clear_pepper_cache

    path = tmp_path / "pepper"
    path.write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
    monkeypatch.setenv(PEPPER_FILE_ENV, str(path))
    clear_pepper_cache()
    yield path
    clear_pepper_cache()


@pytest.fixture
async def db():
    """Create the schema on the test SQLite DB (``_clean_db`` drops it afterwards)."""
    from portunus_gateway.db.models import Base
    from portunus_gateway.db.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def authz(db):
    """Fresh in-process authz store bound as the module singleton for the test."""
    from portunus_gateway import authz as authz_mod

    old = authz_mod.authz_store
    authz_mod.authz_store = authz_mod.AuthzStore()
    yield authz_mod.authz_store
    authz_mod.authz_store = old
