"""Source fingerprint for build identity (attribution / fleet)."""

from __future__ import annotations

import hashlib
from pathlib import Path


def package_fingerprint(package_dir: Path) -> str:
    """First 12 hex chars of SHA-256 over sorted relative ``.py`` paths + contents.

    Computed once at process start for ``serverInfo.version`` / runner build ads.
    Reads the importable tree so overlay deploys change the fingerprint.
    """
    root = package_dir.resolve()
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*.py") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]
