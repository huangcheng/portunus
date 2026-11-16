"""Portunus gateway package.

``__version__`` feeds ``serverInfo.version`` in the MCP ``initialize`` result: clients use it for
compatibility checks and telemetry, so it must never be blank. Format is
``<package version>+<12-hex fingerprint of portunus_gateway>``.
"""

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from portunus_shared import package_fingerprint

log = logging.getLogger("portunus.gateway")

try:
    _pkg_version = version("portunus-gateway")
except PackageNotFoundError:  # source tree without an install (e.g. PYTHONPATH only)
    _pkg_version = "0.0.0"

try:
    _fingerprint = package_fingerprint(Path(__file__).resolve().parent)
except Exception:  # noqa: BLE001 — import must succeed; version degrades to "unknown"
    # Path.resolve() raises RuntimeError on a symlink loop (CPython < 3.13), and
    # the fingerprint walk can fail with more than OSError. Either must not abort import.
    log.warning("gateway package fingerprint failed", exc_info=True)
    _fingerprint = "unknown"

__version__ = f"{_pkg_version.split('+')[0]}+{_fingerprint}"

__all__ = ["__version__"]
