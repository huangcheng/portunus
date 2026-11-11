"""Runner-side spawn: argv re-validation + policy + (optionally) privilege drop.

Spec §3/§6: the gateway validates the closed argv grammar on write/start; the runner
re-validates the *identical* grammar plus ``runner-policy.json`` immediately before
``exec``. The runner is authoritative: nothing the gateway says can widen the policy.

Modes
-----
* ``dev`` (``PORTUNUS_DEV=1``): no uid drop; surplus fds are still closed.
* production: requires ``PORTUNUS_DROP_PRIVS=1`` (fails closed with ``spawn_rejected``
  otherwise) and runs the spec sequence: close surplus fds -> ``setgroups([])`` ->
  ``setgid`` -> ``setuid`` (all via ``subprocess``' ``extra_groups``/``group``/``user``)
  -> clear every capability set (empty ``capset`` + ambient clear) ->
  ``PR_SET_NO_NEW_PRIVS`` -> ``exec``. Needs ``CAP_SETUID|CAP_SETGID`` on Linux, so CI
  skips its end-to-end test unless ``PORTUNUS_DROP_PRIVS=1``.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import pwd
import re
import shutil
import sys
from typing import Any

from portunus_shared import ALLOWED_PORTUNUS_ENV, PortunusError, validate_stdio_command

from .children import ChildHandle, ProcessHandle
from .policy import DEFAULT_POLICY_PATH, Policy, load_policy

DEFAULT_MCP_USER = "portunus-mcp"

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# Runner-owned or loader-injection names that gateway-supplied literals may never set.
_FORBIDDEN_ENV_PREFIXES = ("PORTUNUS_", "LD_", "DYLD_")
_MAX_ENV_ENTRIES = 256
_MAX_ENV_BYTES = 128 * 1024
# StreamReader line limit for child stdout: MCP frames are one JSON line (tools/list with big
# schemas exceeds asyncio's 64KiB default). Stays below the 5MiB wire frame cap.
STDOUT_LIMIT = 4 * 1024 * 1024


def _reject(message: str) -> PortunusError:
    return PortunusError("spawn_rejected", message)


def _str_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise _reject(f"{what} must be a list of strings")
    return value


def validate_env(env: Any) -> dict[str, str]:
    """Literal env from the start frame: string->string, sane names, bounded size."""
    if env is None:
        return {}
    if not isinstance(env, dict) or len(env) > _MAX_ENV_ENTRIES:
        raise _reject("env must be an object of string literals")
    total = 0
    for k, v in env.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise _reject("env must map strings to strings")
        if not _ENV_NAME.fullmatch(k):
            raise _reject(f"invalid env name {k!r}")
        uk = k.upper()
        if uk.startswith(_FORBIDDEN_ENV_PREFIXES) and uk not in ALLOWED_PORTUNUS_ENV:
            raise _reject(f"env name not allowed: {k}")
        if "\x00" in v:
            raise _reject("NUL in env value")
        total += len(k) + len(v)
    if total > _MAX_ENV_BYTES:
        raise _reject("env too large")
    return dict(env)


def resolve_exec(command: str, policy: Policy) -> str:
    """Path to exec for an already-validated ``command``."""
    if command in ("npx", "uvx", "python", "python3"):
        found = shutil.which(command, path=os.pathsep.join(policy.trusted_path))
        if not found:
            raise _reject(f"{command} not found in trusted_path")
        return found
    return os.path.realpath(command)  # exec what was allowlisted, not a swappable symlink


def build_env(command: str, policy: Policy, literals: dict[str, str]) -> dict[str, str]:
    """Scrubbed child env: nothing inherited from the runner (no token), PATH = trusted_path."""
    env = dict(literals)
    env["PATH"] = os.pathsep.join(policy.trusted_path)
    if command == "uvx":
        env["UV_NO_BUILD"] = "1"  # spec §6: not overridable by literals
    return env


# ---- privilege drop (production) ------------------------------------------------------

_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_SYS_CAPSET = {"x86_64": 126, "amd64": 126, "aarch64": 91, "arm64": 91}


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _make_child_hardener():
    """Build the post-setuid hook. Everything it needs is prepared *before* fork so the
    hook itself only makes raw syscalls (it runs in the forked child)."""
    if not sys.platform.startswith("linux"):
        raise PortunusError("spawn_failed", "privilege drop is only supported on Linux")
    nr = _SYS_CAPSET.get(platform.machine().lower())
    if nr is None:
        raise PortunusError("spawn_failed", "unsupported architecture for capset")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    libc.syscall.restype = ctypes.c_long
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()  # all zero: empty effective/permitted/inheritable

    def harden() -> None:
        # Ambient first (empty capset would also drop it, but be explicit), then empty capset.
        if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_CAP_AMBIENT_CLEAR_ALL failed")
        if libc.syscall(nr, ctypes.byref(header), data) != 0:
            raise OSError(ctypes.get_errno(), "capset failed")
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")

    return harden


def drop_privs_kwargs(user: str = DEFAULT_MCP_USER) -> dict[str, Any]:
    """``Popen`` kwargs implementing setgroups([]) -> setgid -> setuid -> caps clear -> NNP."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError as e:
        raise PortunusError("spawn_failed", f"spawn user {user!r} does not exist") from e
    if pw.pw_uid == 0 or pw.pw_gid == 0:
        raise PortunusError("spawn_failed", "refusing to spawn children as root")
    return {
        "user": pw.pw_uid,
        "group": pw.pw_gid,
        "extra_groups": [],  # setgroups([])
        "preexec_fn": _make_child_hardener(),  # runs after setgroups/setgid/setuid
    }


# ---- spawner --------------------------------------------------------------------------


class Spawner:
    """Callable ``(start_frame) -> ChildHandle``; raises ``PortunusError`` on rejection."""

    def __init__(
        self,
        *,
        policy_path: str = DEFAULT_POLICY_PATH,
        dev: bool = False,
        drop_privs: bool = False,
        mcp_user: str = DEFAULT_MCP_USER,
    ) -> None:
        self.policy_path = policy_path
        self.dev = dev
        self.drop_privs = drop_privs and not dev  # dev never setuids
        self.mcp_user = mcp_user

    async def __call__(self, msg: dict) -> ChildHandle:
        argv = _str_list(msg.get("argv"), "argv")
        if not argv:
            raise _reject("empty argv")
        trailing = _str_list(msg.get("trailing_allowlist", []), "trailing_allowlist")
        literals = validate_env(msg.get("env"))
        command, args = argv[0], argv[1:]

        # Re-read the policy on every spawn: validation happens immediately before exec.
        policy = load_policy(self.policy_path, enforce_owner=not self.dev)
        try:
            validate_stdio_command(
                command,
                args,
                trailing_allowlist=trailing,
                trusted_path=list(policy.trusted_path),
                binary_allowlist=list(policy.binary_allowlist),
            )
        except PortunusError:  # includes ArgvError (spawn_rejected)
            raise
        except Exception as e:  # never let a validator bug fall through to exec
            raise _reject(f"argv validation failed: {type(e).__name__}") from e

        if not self.dev and not self.drop_privs:
            raise _reject("privilege drop required outside dev mode (PORTUNUS_DROP_PRIVS=1)")

        exe = resolve_exec(command, policy)
        env = build_env(command, policy, literals)
        extra = drop_privs_kwargs(self.mcp_user) if self.drop_privs else {}
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd="/",
                start_new_session=True,
                close_fds=True,  # surplus fds never reach the child (dev and production)
                pass_fds=(),
                limit=STDOUT_LIMIT,
                **extra,
            )
        except Exception as e:  # OSError, SubprocessError (preexec/harden failure), ValueError
            why = getattr(e, "strerror", None) or type(e).__name__
            raise PortunusError("spawn_failed", f"exec failed: {why}") from e
        return ProcessHandle(proc)
