"""Source fingerprint for build identity (attribution / fleet)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _feed(digest, data: bytes) -> None:
    """Length-prefix ``data`` so NUL bytes inside a file cannot forge an entry boundary."""
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def package_fingerprint(package_dir: Path) -> str:
    """First 12 hex chars of SHA-256 over sorted relative ``.py`` paths + contents.

    Computed once at process start for ``serverInfo.version`` / runner build ads.
    Reads the importable tree so overlay deploys change the fingerprint.
    """
    root = package_dir.resolve(strict=True)
    paths = sorted(p for p in root.rglob("*.py") if p.is_file())
    if not paths:
        raise FileNotFoundError(f"no .py files under {package_dir}")
    digest = hashlib.sha256()
    for path in paths:
        rel = path.relative_to(root).as_posix()
        # fsencode round-trips surrogate-escaped POSIX names; str.encode() does not.
        _feed(digest, os.fsencode(rel))
        _feed(digest, path.read_bytes())
    return digest.hexdigest()[:12]
