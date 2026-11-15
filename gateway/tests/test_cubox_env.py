"""Cubox Extension URL → CUBOX_TOKEN + CUBOX_SERVER normalization."""

from __future__ import annotations

from portunus_gateway.registry import normalize_cubox_env, validate_env


def test_normalize_cubox_pro_save_url():
    out = normalize_cubox_env(
        {"CUBOX_TOKEN": "https://cubox.pro/c/api/save/abcd12345", "CUBOX_SERVER": "cubox.pro"}
    )
    assert out["CUBOX_TOKEN"] == "abcd12345"
    assert out["CUBOX_SERVER"] == "cubox.pro"


def test_normalize_cubox_cc_overrides_default_server():
    out = normalize_cubox_env(
        {"CUBOX_TOKEN": "https://cubox.cc/c/api/save/Tok_en-123", "CUBOX_SERVER": "cubox.pro"}
    )
    assert out["CUBOX_TOKEN"] == "Tok_en-123"
    assert out["CUBOX_SERVER"] == "cubox.cc"


def test_normalize_leaves_bare_token():
    env = {"CUBOX_TOKEN": "bareTokenOnly", "CUBOX_SERVER": "cubox.pro"}
    assert normalize_cubox_env(env) == env


def test_validate_env_applies_cubox_normalize():
    out = validate_env({"CUBOX_TOKEN": "https://cubox.pro/c/api/save/abcXYZ"})
    assert out == {"CUBOX_TOKEN": "abcXYZ", "CUBOX_SERVER": "cubox.pro"}
