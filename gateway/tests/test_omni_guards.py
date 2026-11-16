"""Cursor signature and upstream error-code guards. No runner import."""

from __future__ import annotations

import base64
import json

import pytest
from portunus_shared import PortunusError

from portunus_gateway.omni import _already_gateway_error, decode_cursor, encode_cursor


def test_cursor_rejects_non_ascii_signature(pepper_file):
    cur = encode_cursor("alpha", scope="tools:koofr", query="web")
    assert decode_cursor(cur, scope="tools:koofr", query="web") == "alpha"
    blob = json.dumps(
        {"after": "alpha", "scope": "tools:koofr", "query": "web", "sig": "ü" * 8}
    ).encode()
    bad = base64.urlsafe_b64encode(blob).decode().rstrip("=")
    with pytest.raises(PortunusError) as exc:
        decode_cursor(bad, scope="tools:koofr", query="web")
    assert exc.value.code == "invalid_cursor"


def test_upstream_error_code_must_be_a_string():
    assert _already_gateway_error({"error": {"code": "auth_required"}}) is True
    assert _already_gateway_error({"error": {"code": ["auth_required"]}}) is False
    assert _already_gateway_error({"error": {"code": {"k": "auth_required"}}}) is False
