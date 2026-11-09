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


class ArgvError(PortunusError):
    """Raised when a stdio command/argv violates the closed grammar."""

    def __init__(self, message: str) -> None:
        super().__init__("spawn_rejected", message)


def _reject(message: str) -> ArgvError:
    return ArgvError(message)


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
    if not args or not _UVX_PKG.fullmatch(args[0]):
        raise _reject("uvx requires exactly one package pinned as name==MAJOR.MINOR.PATCH")
    _check_trailing(args[1:], trailing_allowlist)


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

    if os.path.isabs(command):
        real = os.path.realpath(command)
        allowed = {os.path.realpath(p) for p in binary_allowlist}
        if real not in allowed:
            raise _reject("binary not in binary_allowlist")
        for a in args:
            if a not in trailing_allowlist:
                raise _reject(f"arg not in trailing_allowlist: {a!r}")
        return

    raise _reject("command must be npx, uvx, or an allowlisted absolute path")
