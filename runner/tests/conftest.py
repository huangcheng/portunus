import shutil
import tempfile

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sock_path():
    # AF_UNIX paths are short (104 bytes on macOS); pytest's tmp_path can exceed that.
    d = tempfile.mkdtemp(prefix="prt-", dir="/tmp")
    try:
        yield f"{d}/r.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)
