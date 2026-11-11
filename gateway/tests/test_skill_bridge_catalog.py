"""skill_bridge catalog connection + manifest validation."""

from __future__ import annotations

import pytest
from portunus_gateway.catalog import (
    GUANGYAPAN_MANIFEST,
    GUANGYAPAN_SKILL_ROOT,
    GUANGYAPAN_STATE_DIR,
    SKILL_BRIDGE_ARGS,
    SKILL_BRIDGE_COMMAND,
    WEREAD_MANIFEST,
    validate_spec,
)
from portunus_gateway.registry import RegistryError
from portunus_shared.argv import ArgvError, validate_stdio_command


def test_validate_weread_skill_bridge_spec():
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": WEREAD_MANIFEST,
            "secrets": [{"name": "WEREAD_API_KEY", "label": "API key"}],
        },
    )
    assert out["command"] == SKILL_BRIDGE_COMMAND
    assert out["args"] == SKILL_BRIDGE_ARGS
    assert out["manifest"]["backend"] == "http_rpc"
    assert out["secrets"][0]["name"] == "WEREAD_API_KEY"


def test_skill_bridge_requires_auth_secret():
    with pytest.raises(RegistryError):
        validate_spec(
            "skill_bridge",
            {"manifest": WEREAD_MANIFEST, "secrets": []},
        )


def test_validate_guangyapan_skill_bridge_spec():
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": GUANGYAPAN_MANIFEST,
            "secrets": [],
            "env_defaults": {
                "SKILL_ROOT": GUANGYAPAN_SKILL_ROOT,
                "STATE_DIR": GUANGYAPAN_STATE_DIR,
            },
        },
    )
    assert out["manifest"]["backend"] == "json_cli"
    assert out["manifest"]["auth"]["type"] == "device_qr"
    assert out["manifest"]["auth"]["login_env"]["GUANGYAPAN_DRIVE_LOGIN_MODE"] == "device"
    assert out["env_defaults"]["SKILL_ROOT"] == GUANGYAPAN_SKILL_ROOT
    assert {t["name"] for t in out["manifest"]["tools"]} >= {"list", "search", "upload", "status"}


def test_argv_skill_bridge_ok(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: f"/usr/bin/{c}")
    validate_stdio_command(
        "python",
        list(SKILL_BRIDGE_ARGS),
        trailing_allowlist=[],
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )


def test_argv_skill_bridge_rejects_other_module(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: f"/usr/bin/{c}")
    with pytest.raises(ArgvError):
        validate_stdio_command(
            "python",
            ["-m", "evil", "--manifest-env", "X"],
            trailing_allowlist=[],
            trusted_path=["/usr/bin"],
            binary_allowlist=[],
        )
