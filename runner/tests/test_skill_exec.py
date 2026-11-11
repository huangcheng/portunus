"""skill_exec: constrained platform login CLI under /var/lib/portunus/skills."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest import mock

import pytest
from portunus_runner.session import _run_skill_exec
from portunus_shared.errors import PortunusError

pytestmark = pytest.mark.anyio


async def test_skill_exec_rejects_bad_action():
    with pytest.raises(PortunusError) as ei:
        await _run_skill_exec(
            {
                "skill_root": "/var/lib/portunus/skills/demo",
                "state_dir": "/var/lib/portunus/skill-state/demo",
                "entrypoint": "bin/cli.mjs",
                "action": "rm -rf /",
            }
        )
    assert ei.value.code == "spawn_rejected"


async def test_skill_exec_rejects_absolute_entrypoint():
    with pytest.raises(PortunusError) as ei:
        await _run_skill_exec(
            {
                "skill_root": "/var/lib/portunus/skills/demo",
                "state_dir": "/var/lib/portunus/skill-state/demo",
                "entrypoint": "/etc/passwd",
                "action": "login",
            }
        )
    assert ei.value.code == "spawn_rejected"


async def test_skill_exec_runs_and_reads_qr(tmp_path: Path):
    # Patch path prefixes so we can use tmp_path without root.
    with (
        mock.patch("portunus_runner.session._SKILL_ROOT_PREFIX", tmp_path / "skills"),
        mock.patch("portunus_runner.session._STATE_ROOT_PREFIX", tmp_path / "state"),
    ):
        skill_root = tmp_path / "skills" / "demo"
        state_dir = tmp_path / "state" / "demo"
        skill_root.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        script = skill_root / "bin" / "cli.js"
        script.parent.mkdir()
        qr = state_dir / "login-qr.png"
        qr.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
        script.write_text(
            "const path=require('path');\n"
            "const i=process.argv.indexOf('--agent-profile-dir');\n"
            "const dir=i>=0?process.argv[i+1]:'';\n"
            "process.stdout.write(JSON.stringify({\n"
            "  ok:true,\n"
            "  data:{logged_in:false,qr_code_path:path.join(dir,'login-qr.png')}\n"
            "}));\n"
        )

        import shutil

        if not shutil.which("node"):
            pytest.skip("node not installed")

        result = await _run_skill_exec(
            {
                "skill_root": str(skill_root),
                "state_dir": str(state_dir),
                "entrypoint": "bin/cli.js",
                "action": "login",
                "agent_name": "Portunus",
                "agent_profile_dir": str(state_dir),
                "timeout_s": 15,
            }
        )
        assert result["exit_code"] == 0, result
        assert result["result"]["data"]["logged_in"] is False
        assert result["qr_png_base64"] == base64.b64encode(qr.read_bytes()).decode("ascii")
