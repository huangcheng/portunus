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
