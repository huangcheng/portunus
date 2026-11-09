"""Runner spawn policy (spec §3 paths / §6): ``/etc/portunus/runner-policy.json``.

Contents: ``trusted_path`` (dirs used to resolve ``npx``/``uvx``) and ``binary_allowlist``
(realpaths of absolute binaries that may be exec'd). The file is loaded on *every* spawn so
the runner always validates against the policy as it is immediately before ``exec``.
Anything unreadable or malformed fails closed with ``spawn_rejected``.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass

from portunus_shared import PortunusError

DEFAULT_POLICY_PATH = "/etc/portunus/runner-policy.json"

_MAX_POLICY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Policy:
    trusted_path: tuple[str, ...]
    binary_allowlist: tuple[str, ...]


def _reject(message: str) -> PortunusError:
    return PortunusError("spawn_rejected", message)


def _str_list(data: dict, key: str) -> tuple[str, ...]:
    val = data.get(key)
    if not isinstance(val, list) or not all(isinstance(x, str) and x for x in val):
        raise _reject(f"policy.{key} must be a list of non-empty strings")
    if any("\x00" in x or not os.path.isabs(x) for x in val):
        raise _reject(f"policy.{key} entries must be absolute paths")
    return tuple(val)


def parse_policy(data: object) -> Policy:
    if not isinstance(data, dict):
        raise _reject("policy must be a JSON object")
    return Policy(
        trusted_path=_str_list(data, "trusted_path"),
        binary_allowlist=_str_list(data, "binary_allowlist"),
    )


def load_policy(path: str, *, enforce_owner: bool = False) -> Policy:
    """Load and validate the policy file.

    ``enforce_owner`` (production): the file must be root-owned and not group/world
    writable, otherwise a compromised runner-adjacent account could widen the allowlist.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise _reject(f"policy unreadable: {e.strerror}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _reject("policy is not a regular file")
        if enforce_owner and (st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise _reject("policy must be root-owned and not group/world writable")
        if st.st_size > _MAX_POLICY_BYTES:
            raise _reject("policy too large")
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read()
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _reject("policy is not valid JSON") from e
    return parse_policy(data)
