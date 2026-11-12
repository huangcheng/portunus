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


@pytest.mark.anyio
async def test_bearer_env_skill_app_not_flagged_auth_required(db, pepper_file):
    """http_rpc apps (bearer_env) have no login session and must not show auth_required.

    Regression: ``requires_skill_session`` used to be true for every skill_bridge slug,
    so WeRead-style API-key apps were permanently ``auth_required`` despite working.
    """
    import os
    import uuid as _uuid

    from portunus_gateway.db.models import CatalogApp, Server, Tool
    from portunus_gateway.db.session import async_session
    from portunus_gateway.registry import Registry
    from portunus_gateway.runner_pool import RunnerPool
    from portunus_gateway.vault import Vault

    async with async_session() as s:
        s.add(
            CatalogApp(
                id=_uuid.uuid4(),
                slug="bearerdrive",
                label="BearerDrive",
                name="BearerDrive",
                description="API-key skill app",
                connection="skill_bridge",
                spec={
                    "manifest": {"auth": {"type": "bearer_env", "env": "K", "header": "Authorization"}},
                    "secrets": [{"name": "K", "label": "Key", "secret": True}],
                    "env_defaults": {},
                },
                sort_order=1,
            )
        )
        sid = _uuid.uuid4()
        srv = Server(
            id=sid, name="BearerDrive", slug="bearerdrive", kind="stdio", enabled=True,
            command="python", args=["-c", "pass"],
        )
        s.add(srv)
        s.add(Tool(server_id=sid, upstream_name="t", schema={}, schema_hash="h"))
        await s.commit()

    reg = Registry(
        async_session,
        Vault({1: os.urandom(32)}),
        RunnerPool([]),
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
    async with async_session() as s:
        server = (await s.execute(__import__("sqlalchemy").select(Server).where(Server.slug == "bearerdrive"))).scalar_one()
    health = await reg.health_by_server_ids([server])
    # No SKILL_SESSION vault entry, no login ever: still not auth_required — the app's
    # auth model does not have a session to require.
    assert health[sid]["status"] != "auth_required", health[sid]
