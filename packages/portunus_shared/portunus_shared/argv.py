"""Closed argv grammar for stdio servers (spec §6).

Validated by the gateway on write/start and re-validated by the runner
immediately before ``exec`` (runner authoritative).
"""

from __future__ import annotations

import os
import re
from shutil import which

from .errors import PortunusError

_SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+"
_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
_NPX_PKG = re.compile(rf"{_NPM_NAME}@{_SEMVER}", re.ASCII)
_UVX_PKG = re.compile(rf"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?=={_SEMVER}", re.ASCII)

_BASENAMES = frozenset({"npx", "uvx"})
_PYTHON = frozenset({"python", "python3"})
_PIKPAK = frozenset({"pikpak"})
_PIKPAK_MCP_ARGS = ("mcp", "--stdio")
_SKILL_BRIDGE_MODULE = "portunus_skill_bridge"
_SKILL_BRIDGE_ENV_FLAGS = frozenset({"--manifest-env", "--manifest-stdin"})


class ArgvError(PortunusError):
    """Raised when a stdio command/argv violates the closed grammar."""

    def __init__(self, message: str) -> None:
        super().__init__("spawn_rejected", message)


def _reject(message: str) -> ArgvError:
    return ArgvError(message)


_WIN_EXTS = frozenset({".exe", ".cmd", ".bat"})


def _program_name(real: str) -> str:
    """Basename of an allowlisted realpath, without a Windows launcher extension."""
    base = os.path.basename(real)
    stem, ext = os.path.splitext(base)
    if ext.casefold() in _WIN_EXTS:
        base = stem
    return base.casefold()


def _is_python_basename(name: str) -> bool:
    """``python``, ``python3``, and versioned interpreters (``python3.12``)."""
    if name in _PYTHON:
        return True
    if not name.startswith("python"):
        return False
    rest = name[len("python") :]
    return bool(rest) and all(part.isdigit() for part in rest.split("."))


def _check_trailing(args: list[str], trailing_allowlist: list[str]) -> None:
    for a in args:
        if a.startswith("-") and a not in trailing_allowlist:
            raise _reject(f"trailing arg not allowed: {a!r}")


def _validate_npx(args: list[str], trailing_allowlist: list[str]) -> None:
    i = 0
    if i < len(args) and args[i] in ("-y", "--yes"):
        i += 1
    if i >= len(args) or args[i] != "--ignore-scripts":
        raise _reject("npx requires --ignore-scripts")
    i += 1
    if i >= len(args) or not _NPX_PKG.fullmatch(args[i]):
        raise _reject("npx requires exactly one package pinned as name@MAJOR.MINOR.PATCH")
    _check_trailing(args[i + 1 :], trailing_allowlist)


def _validate_uvx(args: list[str], trailing_allowlist: list[str]) -> None:
    """``uvx [--with name==semver]… name==semver [trailing…]``.

    Leading ``--with`` pins are for transitive deps (e.g. pin ``mcp`` for an
    older server package). The primary tool package remains required and pinned.
    """
    i = 0
    while i + 1 < len(args) and args[i] == "--with":
        if not _UVX_PKG.fullmatch(args[i + 1]):
            raise _reject("uvx --with requires name==MAJOR.MINOR.PATCH")
        i += 2
    if i >= len(args) or not _UVX_PKG.fullmatch(args[i]):
        raise _reject("uvx requires exactly one package pinned as name==MAJOR.MINOR.PATCH")
    _check_trailing(args[i + 1 :], trailing_allowlist)


def validate_stdio_command(
    command: str,
    args: list[str],
    *,
    trailing_allowlist: list[str],
    trusted_path: list[str],
    binary_allowlist: list[str],
) -> None:
    """Validate ``command``/``args`` against the closed argv grammar.

    Raises ``ArgvError`` (code ``spawn_rejected``) on any violation.
    """
    if not isinstance(command, str) or not command or "\x00" in command:
        raise _reject("invalid command")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise _reject("args must be a list of strings")
    if any("\x00" in a for a in args):
        raise _reject("NUL in args")

    if command in _BASENAMES:
        resolved = which(command, path=os.pathsep.join(trusted_path))
        if not resolved:
            raise _reject(f"{command} not found in trusted_path")
        if command == "npx":
            _validate_npx(args, trailing_allowlist)
        else:
            _validate_uvx(args, trailing_allowlist)
        return

    if command in _PYTHON:
        resolved = which(command, path=os.pathsep.join(trusted_path))
        if not resolved:
            raise _reject(f"{command} not found in trusted_path")
        _validate_python_skill_bridge(args)
        return

    if command in _PIKPAK:
        resolved = which(command, path=os.pathsep.join(trusted_path))
        if not resolved:
            raise _reject(f"{command} not found in trusted_path")
        if tuple(args) != _PIKPAK_MCP_ARGS:
            raise _reject("pikpak requires exactly: mcp --stdio")
        return

    if os.path.isabs(command):
        real = os.path.realpath(command)
        allowed = {os.path.realpath(p) for p in binary_allowlist}
        if real not in allowed:
            raise _reject("binary not in binary_allowlist")
        # The same binary must not skip the closed grammar just because the
        # operator spelled it as an absolute path that happens to be allowlisted.
        name = _program_name(real)
        if name == "npx":
            _validate_npx(args, trailing_allowlist)
            return
        if name == "uvx":
            _validate_uvx(args, trailing_allowlist)
            return
        if name == "pikpak":
            if tuple(args) != _PIKPAK_MCP_ARGS:
                raise _reject("pikpak requires exactly: mcp --stdio")
            return
        if _is_python_basename(name):
            _validate_python_skill_bridge(args)
            return
        for a in args:
            if a not in trailing_allowlist:
                raise _reject(f"arg not in trailing_allowlist: {a!r}")
        return

    raise _reject(
        "command must be npx, uvx, pikpak mcp --stdio, python -m portunus_skill_bridge, "
        "or an allowlisted absolute path"
    )


def _validate_python_skill_bridge(args: list[str]) -> None:
    """Only ``python -m portunus_skill_bridge --manifest-env VAR`` (or ``--manifest-stdin``)."""
    if len(args) < 2 or args[0] != "-m" or args[1] != _SKILL_BRIDGE_MODULE:
        raise _reject("python only allows -m portunus_skill_bridge")
    rest = args[2:]
    if not rest or rest[0] not in _SKILL_BRIDGE_ENV_FLAGS:
        raise _reject("portunus_skill_bridge requires --manifest-env VAR or --manifest-stdin")
    if rest[0] == "--manifest-stdin":
        if len(rest) != 1:
            raise _reject("unexpected args after --manifest-stdin")
        return
    # --manifest-env VAR
    if len(rest) != 2 or not rest[1] or rest[1].startswith("-") or "\x00" in rest[1]:
        raise _reject("--manifest-env requires a single env var name")
