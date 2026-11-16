"""Unit tests for the rclone connector shim's config parser (deploy/rclone-shim/shim)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_SHIM = Path(__file__).resolve().parents[2] / "deploy" / "rclone-shim" / "shim"
# No .py extension: give importlib an explicit source loader.
_loader = importlib.machinery.SourceFileLoader("rclone_shim", str(_SHIM))
_spec = importlib.util.spec_from_loader("rclone_shim", _loader)
shim = importlib.util.module_from_spec(_spec)
_loader.exec_module(shim)


def test_parses_section_without_header():
    cfg = shim.parse_config("type = box\ntoken = {\"a\": 1}\n")
    assert cfg == {"type": "box", "token": '{"a": 1}'}


def test_header_and_comments_and_semicolon_comments_ignored():
    cfg = shim.parse_config("[box]\n# comment\n; comment\ntype = box\n")
    assert cfg["type"] == "box" and len(cfg) == 1


def test_missing_type_fails():
    with pytest.raises(SystemExit):
        shim.parse_config("token = x\n")


def test_duplicate_keys_last_wins():
    cfg = shim.parse_config("type = box\ntoken = one\ntoken = two\n")
    assert cfg["token"] == "two"


def test_value_containing_equals():
    cfg = shim.parse_config("type = box\ntoken = {\"a\": \"b=c\"}\n")
    assert cfg["token"] == '{"a": "b=c"}'


def test_base64_value_decoded():
    import base64

    enc = base64.b64encode("line1\nline2".encode()).decode()
    cfg = shim.parse_config(f"type = box\nkey = base64;{enc}\n")
    assert cfg["key"] == "line1\nline2"


def test_bad_base64_fails():
    with pytest.raises(SystemExit):
        shim.parse_config("type = box\nkey = base64;!!!not-base64!!!\n")


def test_control_chars_fail():
    with pytest.raises(SystemExit):
        shim.parse_config("type = box\nkey = bad\x01value\n")


def test_backend_allowlist():
    assert "box" in shim.ALLOWED_BACKENDS and "onedrive" in shim.ALLOWED_BACKENDS
    assert "yandex" in shim.ALLOWED_BACKENDS and "drime" in shim.ALLOWED_BACKENDS
    assert "mega" in shim.ALLOWED_BACKENDS
    assert "local" not in shim.ALLOWED_BACKENDS and "alias" not in shim.ALLOWED_BACKENDS


def test_fail_does_not_echo_config_lines():
    """fail() must not print offending config content (stderr reaches shared logs)."""
    import contextlib
    import io

    buf = io.StringIO()
    with pytest.raises(SystemExit), contextlib.redirect_stderr(buf):
        shim.parse_config("type = box\nthis-line-is-bad\n")
    assert "this-line-is-bad" not in buf.getvalue()


def test_mcp_package_pin_matches_catalog():
    """The shim's webdav-mcp-server pin must match the catalog's WebDAV seed pin."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from portunus_gateway.catalog import WEBDAV_MCP_PACKAGE

    assert shim.MCP_PACKAGE == WEBDAV_MCP_PACKAGE


def test_multiline_value_write_reencodes(tmp_path):
    """Multiline config values are stored base64-encoded in the conf file (rclone
    config format is line-based) and decoded again by parse_config."""
    import base64

    from portunus_gateway.catalog import DEFAULT_APPS  # noqa: F401 (imports shim deps)

    multiline = "line1\nline2"
    enc = "base64;" + base64.b64encode(multiline.encode()).decode()
    cfg = shim.parse_config(f"type = box\nkey = {enc}\n")
    assert cfg["key"] == multiline


def test_mega_session_drops_pass_and_2fa(monkeypatch, tmp_path):
    """session_id+master_key wins; plaintext pass must not reach the conf file."""
    rclone = tmp_path / "rclone"
    rclone.write_text("#!/bin/sh\n")

    def _no_obscure(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("obscure not needed when session credentials present")

    monkeypatch.setattr(shim, "ensure_obscured", _no_obscure)
    cfg = shim.normalize_config(
        {
            "type": "mega",
            "user": "u@example.com",
            "pass": "plaintext",
            "2fa": "123456",
            "session_id": "sess",
            "master_key": "mk==",
        },
        rclone,
    )
    assert "pass" not in cfg and "2fa" not in cfg
    assert cfg["session_id"] == "sess" and cfg["master_key"] == "mk=="


def test_ensure_obscured_passes_through_real_obscured(monkeypatch, tmp_path):
    """Round-trip with rclone's public obscure algorithm (in-process reveal)."""
    import base64

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    rclone = tmp_path / "rclone"
    rclone.write_text("#!/bin/sh\n")
    iv = b"\x01" * 16
    pt = b"secret-pass"
    enc = Cipher(algorithms.AES(shim._RCLONE_OBSCURE_KEY), modes.CTR(iv)).encryptor()
    ct = enc.update(pt) + enc.finalize()
    blob = base64.urlsafe_b64encode(iv + ct).decode().rstrip("=")

    def _no_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("subprocess must not run for already-obscured values")

    monkeypatch.setattr(shim.subprocess, "run", _no_run)
    assert shim.ensure_obscured(rclone, blob) == blob


def test_ensure_obscured_obscures_plaintext(monkeypatch, tmp_path):
    rclone = tmp_path / "rclone"
    rclone.write_text("#!/bin/sh\n")

    class _R:
        def __init__(self, code: int, out: str = ""):
            self.returncode = code
            self.stdout = out

    calls: list[str] = []

    def _run(argv, **_k):
        calls.append(argv[1])
        assert argv[1] == "obscure" and argv[2] == "-"
        assert _k.get("input") == "plaintext\n"
        return _R(0, "OBSCURED\n")

    monkeypatch.setattr(shim.subprocess, "run", _run)
    assert shim.ensure_obscured(rclone, "plaintext") == "OBSCURED"
    assert calls == ["obscure"]


def test_ensure_obscured_reobscures_garbage_base64(monkeypatch, tmp_path):
    """Random base64url ≥16 bytes decrypts to garbage → obscure via stdin."""
    rclone = tmp_path / "rclone"
    rclone.write_text("#!/bin/sh\n")

    class _R:
        def __init__(self, code: int, out: str = ""):
            self.returncode = code
            self.stdout = out

    def _run(argv, **_k):
        assert argv[1] == "obscure"
        return _R(0, "OBSCURED\n")

    monkeypatch.setattr(shim.subprocess, "run", _run)
    # 32 zero bytes → CTR decrypt of empty-ish / non-utf8 or non-printable.
    blob = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert shim.ensure_obscured(rclone, blob) == "OBSCURED"
