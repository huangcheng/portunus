from pathlib import Path

import pytest

from portunus_skill_bridge.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
WEREAD = ROOT / "manifests" / "weread.example.yaml"
GUANG = ROOT / "manifests" / "guangyapan.example.yaml"
QUARK = ROOT / "manifests" / "quark.example.yaml"
CUBOX = ROOT / "manifests" / "cubox.example.yaml"
FLYAI = ROOT / "manifests" / "flyai.example.yaml"
IMA = ROOT / "manifests" / "ima.example.yaml"
QQMUSIC = ROOT / "manifests" / "qqmusic.example.yaml"
PROTON = ROOT / "manifests" / "proton-drive.example.yaml"
AGENT_MAIL = ROOT / "manifests" / "agent-mail.example.yaml"


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
    assert m.auth.login_env == {}
    assert m.auth.actions.status == "get-user-info"
    assert m.cli is not None
    assert m.cli.run_wrapper == "raw"
    assert m.cli.identity.agent_name is None
    assert {t.name for t in m.tools} >= {"search", "browse", "get_user_info", "download"}


def test_load_cubox_example():
    m = load_manifest(CUBOX)
    assert m.backend == "json_cli"
    assert m.auth.type == "none"
    assert m.cli is not None
    assert m.cli.run_wrapper == "raw"
    assert m.cli.command == ["/usr/local/bin/cubox-cli"]
    assert m.cli.provision is not None
    assert m.cli.provision.repo == "OLCUBO/cubox-cli"
    assert m.cli.provision.tag == "v1.0.10"
    assert "{version}" in m.cli.provision.asset
    folder = next(t for t in m.tools if t.name == "folder_list")
    assert folder.cli == ["folder", "list"]
    assert {t.name for t in m.tools} >= {"folder_list", "card_list", "card_rag", "save"}


def test_load_proton_drive_example():
    m = load_manifest(PROTON)
    assert m.backend == "json_cli"
    assert m.auth.type == "oauth_link"
    assert m.auth.wants_browser_capture()
    assert m.auth.token_paste is False
    assert m.cli is not None
    assert m.cli.command == ["node", "scripts/proton-drive.cjs"]
    assert m.cli.provision is not None
    assert m.cli.provision.kind == "http_file"
    assert m.cli.provision.dest == "/usr/local/bin/proton-drive"
    assert {
        "fs_list",
        "fs_upload",
        "fs_download",
        "sharing_status",
        "photo_timeline",
        "album_list",
        "album_photos",
    } <= {t.name for t in m.tools}
    assert "login" not in {t.name for t in m.tools}


def test_load_himalaya_example():
    m = load_manifest(ROOT / "manifests" / "himalaya.example.yaml")
    assert m.backend == "json_cli"
    assert m.auth.type == "none"
    assert m.cli is not None
    assert m.cli.command == ["/var/lib/portunus/skills/himalaya-shim/run"]
    assert m.cli.provision is not None
    assert m.cli.provision.repo == "pimalaya/himalaya"
    assert "{gnu_arch}" in (m.cli.provision.asset or "")
    assert {
        "mailbox_list",
        "envelope_list",
        "envelope_search",
        "message_read",
        "message_send",
    } <= {t.name for t in m.tools}



def test_load_agent_mail_example():
    m = load_manifest(AGENT_MAIL)
    assert m.backend == "json_cli"
    assert m.auth.type == "oauth_link"
    assert m.auth.wants_browser_capture()
    assert m.auth.token_paste is False
    assert m.cli is not None
    assert m.cli.command == ["node", "scripts/agent-mail.cjs"]
    assert m.cli.provision is None
    assert m.cli.identity.agent_name is None
    assert {
        "me",
        "message_list",
        "message_read",
        "message_search",
        "message_send",
        "message_reply",
        "message_forward",
        "message_trash",
        "message_delete",
        "attachment_upload",
        "attachment_download",
    } <= {t.name for t in m.tools}
    assert "login" not in {t.name for t in m.tools}
    send = next(t for t in m.tools if t.name == "message_send")
    assert "confirmation_token" in send.input


def test_absolute_cli_requires_provision():
    from portunus_skill_bridge.manifest import Manifest

    with pytest.raises(Exception, match="absolute"):
        Manifest.model_validate(
            {
                "name": "x",
                "backend": "json_cli",
                "cli": {"command": ["/usr/local/bin/missing-cli"]},
                "tools": [{"name": "t", "description": "d", "cli": "t"}],
            }
        )


def test_provision_requires_absolute_dest():
    from portunus_skill_bridge.manifest import Manifest

    with pytest.raises(Exception, match="absolute"):
        Manifest.model_validate(
            {
                "name": "x",
                "backend": "json_cli",
                "cli": {
                    "command": ["node", "scripts/x.cjs"],
                    "provision": {
                        "kind": "github_release",
                        "repo": "OLCUBO/cubox-cli",
                        "tag": "v1.0.10",
                        "asset": "cubox-cli-{version}-linux-{arch}.tar.gz",
                    },
                },
                "tools": [{"name": "t", "description": "d", "cli": "t"}],
            }
        )


def test_http_file_provision_with_node_wrapper():
    from portunus_skill_bridge.manifest import Manifest

    m = Manifest.model_validate(
        {
            "name": "x",
            "backend": "json_cli",
            "cli": {
                "command": ["node", "scripts/x.cjs"],
                "provision": {
                    "kind": "http_file",
                    "url": "https://example.com/bin/{arch}",
                    "sha512": "a" * 128,
                    "dest": "/usr/local/bin/x",
                    "binary": "x",
                },
            },
            "tools": [{"name": "t", "description": "d", "cli": "t"}],
        }
    )
    assert m.cli is not None
    assert m.cli.provision is not None
    assert m.cli.provision.dest == "/usr/local/bin/x"


def test_load_flyai_example():
    m = load_manifest(FLYAI)
    assert m.backend == "json_cli"
    assert m.auth.type == "none"
    assert m.cli is not None
    assert m.cli.run_wrapper == "raw"
    assert m.cli.command[:2] == ["npx", "-y"]
    assert "@fly-ai/flyai-cli" in m.cli.command[2]
    flight = next(t for t in m.tools if t.name == "search_flight")
    assert flight.cli == ["search-flight"]
    assert flight.input["origin"].required is True
    assert {t.name for t in m.tools} >= {
        "keyword_search",
        "ai_search",
        "search_flight",
        "search_hotel",
        "search_poi",
    }


def test_load_ima_example():
    m = load_manifest(IMA)
    assert m.backend == "http_rpc"
    assert m.http is not None
    assert m.http.dispatch_mode == "url_path"
    assert m.auth.type == "header_env"
    assert m.auth.env == "IMA_OPENAPI_APIKEY"
    assert m.auth.header == "ima-openapi-apikey"
    assert m.auth.extra_headers_env["ima-openapi-clientid"] == "IMA_OPENAPI_CLIENTID"
    assert {t.name for t in m.tools} >= {
        "search_note",
        "import_doc",
        "search_knowledge_base",
        "import_urls",
    }


def test_load_qqmusic_example():
    m = load_manifest(QQMUSIC)
    assert m.backend == "http_rpc"
    assert m.http is not None
    assert m.http.dispatch_mode == "url_path"
    assert m.http.body_envelope == "params_comm"
    assert m.http.const["skill_version"] == "0.0.3"
    assert m.auth.type == "bearer_env"
    assert m.auth.env == "QQMUSIC_API_KEY"
    search = next(t for t in m.tools if t.name == "discover_search")
    assert search.input["search_type"].wire == "type"
    assert {t.name for t in m.tools} >= {
        "discover_search",
        "discover_daily_mix",
        "charts",
        "me_report",
    }


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


def test_http_header_value_rejects_crlf_and_nul():
    from portunus_skill_bridge.manifest import Manifest

    base = {
        "name": "x",
        "backend": "http_rpc",
        "http": {"base_url": "https://example.com", "headers": {}},
        "tools": [{"name": "t", "description": "d", "dispatch": "t"}],
    }
    for value in ("a\r\nAuthorization: Bearer x", "a\nb", "a\x00b"):
        spec = {
            **base,
            "http": {"base_url": "https://example.com", "headers": {"X-A": value}},
        }
        with pytest.raises(Exception, match="invalid header value"):
            Manifest.model_validate(spec)


def test_manifest_rejects_bad_timeouts_urls_headers_and_field_names():
    from portunus_skill_bridge.manifest import Manifest

    base = {
        "name": "x",
        "backend": "http_rpc",
        "auth": {"type": "bearer_env", "env": "TOKEN"},
        "http": {"base_url": "https://example.com"},
        "tools": [{"name": "t", "description": "d", "dispatch": "t", "input": {}}],
    }
    for timeout in (0, -1, float("nan"), float("inf")):
        spec = {**base, "http": {"base_url": "https://example.com", "timeout_s": timeout}}
        with pytest.raises(Exception):
            Manifest.model_validate(spec)
    for url in ("", "   ", "/relative", "ftp://example.com/x"):
        spec = {**base, "http": {"base_url": url}}
        with pytest.raises(Exception):
            Manifest.model_validate(spec)
    spec = {**base, "auth": {"type": "bearer_env", "env": "TOKEN", "header": "X\r\nInjected"}}
    with pytest.raises(Exception, match="invalid header name"):
        Manifest.model_validate(spec)
    spec = {
        **base,
        "tools": [
            {
                "name": "t",
                "description": "d",
                "dispatch": "t",
                "input": {"user-name": {"type": "string"}},
            }
        ],
    }
    with pytest.raises(Exception, match="not a valid parameter name"):
        Manifest.model_validate(spec)
    nan_auth = {
        "name": "x",
        "backend": "json_cli",
        "auth": {"type": "device_qr", "capture_timeout_s": float("nan")},
        "cli": {"command": ["echo"]},
        "tools": [{"name": "t", "description": "d", "cli": "t"}],
    }
    with pytest.raises(Exception):
        Manifest.model_validate(nan_auth)


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
