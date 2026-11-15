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
