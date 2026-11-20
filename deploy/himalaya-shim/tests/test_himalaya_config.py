"""Unit tests for himalaya-shim config + argv rewrite (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]


def _load_config():
    path = HERE / "himalaya_config.py"
    spec = importlib.util.spec_from_file_location("himalaya_config", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["himalaya_config"] = mod
    spec.loader.exec_module(mod)
    return mod


cfg = _load_config()


def test_build_config_embeds_urls_and_address():
    text = cfg.build_config(
        imap_url="imaps://imap.qq.com:993",
        smtp_url="smtps://smtp.qq.com:465",
        address="a@qq.com",
        password="secret",
    )
    assert 'imap.server = "imaps://imap.qq.com:993"' in text
    assert 'smtp.server = "smtps://smtp.qq.com:465"' in text
    assert 'imap.sasl.plain.username = "a@qq.com"' in text
    assert 'password.raw = "secret"' in text
    assert "default = true" in text


def test_build_config_escapes_password_quotes_and_backslash():
    text = cfg.build_config(
        imap_url="imaps://imap.example:993",
        smtp_url="smtps://smtp.example:465",
        address="u@example.com",
        password=r'p"ass\word',
    )
    assert r'password.raw = "p\"ass\\word"' in text


def test_build_config_rejects_empty_fields():
    with pytest.raises(ValueError, match="IMAP_URL"):
        cfg.build_config(
            imap_url="  ",
            smtp_url="smtps://x",
            address="a@b.c",
            password="x",
        )


def test_build_config_from_env_lists_missing():
    with pytest.raises(ValueError, match="MAIL_PASSWORD"):
        cfg.build_config_from_env(
            {
                "IMAP_URL": "imaps://x",
                "SMTP_URL": "smtps://y",
                "MAIL_ADDRESS": "a@b.c",
            }
        )


def test_rewrite_argv_moves_id_for_message_read():
    assert cfg.rewrite_argv(["message", "read", "--id", "42", "--mailbox", "INBOX"]) == [
        "message",
        "read",
        "--mailbox",
        "INBOX",
        "42",
    ]


def test_rewrite_argv_keeps_id_flag_when_not_positional():
    assert cfg.rewrite_argv(["envelope", "list", "--id", "nope"]) == [
        "envelope",
        "list",
        "--id",
        "nope",
    ]


def test_rewrite_argv_moves_query_for_envelope_search():
    assert cfg.rewrite_argv(
        ["envelope", "search", "--query", "from:alice", "--mailbox", "INBOX"]
    ) == [
        "envelope",
        "search",
        "--mailbox",
        "INBOX",
        "from:alice",
    ]


def test_rewrite_argv_maps_source_mailbox_to_from():
    assert cfg.rewrite_argv(
        ["message", "move", "--id", "9", "--source-mailbox", "INBOX", "--to", "Archive"]
    ) == [
        "message",
        "move",
        "--from",
        "INBOX",
        "--to",
        "Archive",
        "9",
    ]
