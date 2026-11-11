from pathlib import Path

import pytest

from portunus_skill_bridge.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
WEREAD = ROOT / "manifests" / "weread.example.yaml"
GUANG = ROOT / "manifests" / "guangyapan.example.yaml"
QUARK = ROOT / "manifests" / "quark.example.yaml"


def test_load_weread_example():
    m = load_manifest(WEREAD)
    assert m.backend == "http_rpc"
    assert m.http is not None
    assert m.auth.env == "WEREAD_API_KEY"
    assert {t.name for t in m.tools} >= {"search_books", "shelf_sync"}


def test_load_guangyapan_example():
    m = load_manifest(GUANG)
    assert m.backend == "json_cli"
    assert m.cli is not None
    assert m.auth.type == "device_qr"
    assert m.auth.is_device_qr()
    assert m.auth.login_env.get("GUANGYAPAN_DRIVE_LOGIN_MODE") == "device"
    assert m.auth.qr_path_field == "qr_code_path"
    assert m.auth.actions.status == "login-status"
    assert {t.name for t in m.tools} >= {"list", "search", "upload", "download", "status"}


def test_load_quark_example():
    m = load_manifest(QUARK)
    assert m.backend == "json_cli"
    assert m.auth.type == "oauth_link"
    assert m.auth.is_oauth_link()
    assert m.auth.wants_browser_capture()
    assert m.auth.capture_timeout_s == 90
    assert m.auth.poll_interval_s == 2
    assert m.auth.token_paste is True
    assert m.auth.connect_hint and "打开授权页" in m.auth.connect_hint
    assert m.auth.login_env.get("OPENCLAW_CLI") == "1"
    assert m.auth.actions.status == "get-user-info"
    assert m.cli is not None
    assert m.cli.run_wrapper == "raw"
    assert m.cli.identity.agent_name is None
    assert {t.name for t in m.tools} >= {"search", "browse", "get_user_info", "download"}


def test_oauth_link_browser_capture_defaults_off():
    from portunus_skill_bridge.manifest import Manifest

    m = Manifest.model_validate(
        {
            "name": "x",
            "backend": "json_cli",
            "auth": {"type": "oauth_link"},
            "cli": {"command": ["node", "cli.cjs"]},
            "tools": [{"name": "t", "description": "d", "cli": "t"}],
        }
    )
    assert m.auth.is_oauth_link()
    assert m.auth.browser_capture is False
    assert m.auth.wants_browser_capture() is False
    assert m.auth.token_paste is True


def test_device_qr_requires_json_cli():
    from portunus_skill_bridge.manifest import Manifest

    with pytest.raises(Exception):
        Manifest.model_validate(
            {
                "name": "x",
                "backend": "http_rpc",
                "auth": {"type": "device_qr"},
                "http": {"base_url": "https://example.com"},
                "tools": [{"name": "t", "description": "d", "dispatch": "t"}],
            }
        )


def test_external_session_alias_is_device_qr():
    from portunus_skill_bridge.manifest import Manifest

    m = Manifest.model_validate(
        {
            "name": "x",
            "backend": "json_cli",
            "auth": {"type": "external_session"},
            "cli": {"command": ["node", "bin/cli.mjs"]},
            "tools": [{"name": "t", "description": "d", "operation": "t"}],
        }
    )
    assert m.auth.is_device_qr()


def test_http_rpc_requires_dispatch():
    from portunus_skill_bridge.manifest import Manifest

    with pytest.raises(Exception):
        Manifest.model_validate(
            {
                "name": "x",
                "backend": "http_rpc",
                "http": {"base_url": "https://example.com"},
                "tools": [{"name": "t", "description": "d"}],
            }
        )
