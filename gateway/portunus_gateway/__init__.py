"""Portunus gateway package.

``__version__`` feeds ``serverInfo.version`` in the MCP ``initialize`` result: clients use it for
compatibility checks and telemetry, so it must never be blank.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("portunus-gateway")
except PackageNotFoundError:  # source tree without an install (e.g. PYTHONPATH only)
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
