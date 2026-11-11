"""Portunus gateway package.

``__version__`` feeds ``serverInfo.version`` in the MCP ``initialize`` result: clients use it for
compatibility checks and telemetry, so it must never be blank. Format is
``<package version>+<12-hex fingerprint of portunus_gateway>``.
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from portunus_shared import package_fingerprint

try:
    _pkg_version = version("portunus-gateway")
except PackageNotFoundError:  # source tree without an install (e.g. PYTHONPATH only)
    _pkg_version = "0.0.0"

try:
    _fingerprint = package_fingerprint(Path(__file__).resolve().parent)
except OSError:
    _fingerprint = "unknown"

__version__ = f"{_pkg_version.split('+')[0]}+{_fingerprint}"

__all__ = ["__version__"]
