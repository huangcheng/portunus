"""package_fingerprint: stable for identical trees, changes when a .py file changes."""

from __future__ import annotations

from pathlib import Path

from portunus_shared import package_fingerprint


def test_fingerprint_stable_and_changes_with_content(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("y = 2\n", encoding="utf-8")
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("z = 3\n", encoding="utf-8")

    first = package_fingerprint(pkg)
    assert len(first) == 12
    assert all(c in "0123456789abcdef" for c in first)
    assert package_fingerprint(pkg) == first

    (pkg / "a.py").write_text("x = 2\n", encoding="utf-8")
    second = package_fingerprint(pkg)
    assert second != first
    assert len(second) == 12


def test_fingerprint_ignores_non_py(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("ok\n", encoding="utf-8")
    before = package_fingerprint(pkg)
    (pkg / "readme.md").write_text("noise\n", encoding="utf-8")
    (pkg / "a.pyc").write_bytes(b"\0\1\2")
    assert package_fingerprint(pkg) == before
