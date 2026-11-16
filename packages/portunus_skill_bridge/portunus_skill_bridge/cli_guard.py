"""Validate skill-bridge ``cli.command`` — no shell / eval argv (audit 2026-11-16 Finding 4)."""

from __future__ import annotations

import os
import re

_LITERAL_HEADS = frozenset({"node", "npx"})
_SHELL_BASENAMES = frozenset(
    {"sh", "bash", "dash", "zsh", "csh", "tcsh", "ksh", "fish", "busybox"}
)
_EVAL_FLAGS = frozenset(
    {
        "-c",
        "-e",
        "-p",
        "-r",
        "--eval",
        "--print",
        "--run",
        "--exec",
        "--require",
        "--import",
        "--loader",
        "--experimental-loader",
    }
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_BASE_ENV_KEEP = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "STATE_DIR",
        "SKILL_ROOT",
        "NODE_ENV",
        "NODE_PATH",
        "USER",
        "LOGNAME",
        "PORTUNUS_MANIFEST",
    }
)


def _forbid_eval_tokens(tokens: list[str], *, where: str) -> None:
    for a in tokens:
        if a in _EVAL_FLAGS or a.split("=", 1)[0] in _EVAL_FLAGS:
            raise ValueError(f"{where} forbids eval-style flag {a!r}")
        if a.startswith(("-e", "-c", "-p")) and not a.startswith("--") and len(a) > 2:
            raise ValueError(f"{where} forbids eval-style flag {a!r}")


def validate_cli_argv_tail(tokens: list[str] | str | None, *, where: str = "tool.cli") -> None:
    """Reject eval/shell smuggling in per-tool ``cli`` argv suffixes."""
    if tokens is None:
        return
    if isinstance(tokens, str):
        parts = [tokens] if tokens else []
    elif isinstance(tokens, list):
        parts = tokens
    else:
        raise ValueError(f"{where} must be a string or list of strings")
    if not all(isinstance(a, str) and a and "\x00" not in a for a in parts):
        raise ValueError(f"{where} entries must be non-empty strings without NUL")
    for a in parts:
        if a in _SHELL_BASENAMES or os.path.basename(a).casefold() in _SHELL_BASENAMES:
            raise ValueError(f"{where} forbids shell interpreter {a!r}")
    _forbid_eval_tokens(parts, where=where)


def validate_cli_command(command: list[str]) -> None:
    """Raise ``ValueError`` if ``cli.command`` is outside the closed skill-CLI grammar."""
    if not isinstance(command, list) or not command:
        raise ValueError("cli.command must be a non-empty list of strings")
    if not all(isinstance(a, str) and a and "\x00" not in a for a in command):
        raise ValueError("cli.command entries must be non-empty strings without NUL")

    head = command[0]
    if head.startswith("-"):
        raise ValueError(f"cli.command head must not be a flag: {head!r}")

    base = os.path.basename(head).casefold()
    stem, ext = os.path.splitext(base)
    if ext in {".exe", ".cmd", ".bat"}:
        base = stem

    if base in _SHELL_BASENAMES:
        raise ValueError(f"shell interpreter not allowed in cli.command: {head!r}")

    if os.path.isabs(head):
        pass
    elif base not in _LITERAL_HEADS:
        raise ValueError(
            "cli.command[0] must be 'node', 'npx', or an absolute path "
            f"(got {head!r})"
        )

    for a in command[1:]:
        if a in _SHELL_BASENAMES or os.path.basename(a).casefold() in _SHELL_BASENAMES:
            raise ValueError(f"cli.command forbids shell interpreter {a!r}")
    _forbid_eval_tokens(command[1:], where="cli.command")


def scrubbed_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Forward parent env except runner-owned prefixes (PORTUNUS_/LD_/DYLD_)."""
    src = dict(os.environ) if source is None else dict(source)
    out: dict[str, str] = {}
    for k, v in src.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if not _ENV_NAME.fullmatch(k):
            continue
        uk = k.upper()
        if uk.startswith(("LD_", "DYLD_")):
            continue
        if uk.startswith("PORTUNUS_") and k not in _BASE_ENV_KEEP:
            continue
        out[k] = v
    if "PATH" not in out:
        out["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return out
