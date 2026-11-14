import json
from pathlib import Path

import pytest

from portunus_skill_bridge.backends.json_cli import JsonCliBackend
from portunus_skill_bridge.manifest import load_manifest

GUANG = Path(__file__).resolve().parents[1] / "manifests" / "guangyapan.example.yaml"


@pytest.mark.anyio
async def test_json_cli_run_operation_stdin(monkeypatch, tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "bin").mkdir()
    # Fake node binary via a shell script recorded as argv[0] replacement:
    # JsonCliBackend runs command as-is; point command at a python recorder.
    recorder = tmp_path / "recorder.py"
    recorder.write_text(
        "\n".join(
            [
                "import json,sys",
                "stdin=sys.stdin.read()",
                "print(json.dumps({\"argv\": sys.argv[1:], \"stdin\": json.loads(stdin)}))",
            ]
        ),
        encoding="utf-8",
    )
    m = load_manifest(GUANG)
    # Override CLI to our recorder (still exercises run_operation path).
    m.cli.command = [sys_executable(), str(recorder)]  # type: ignore[union-attr]
    m.cli.cwd = str(skill)  # type: ignore[union-attr]
    m.cli.identity.agent_profile_dir = str(tmp_path / "state")  # type: ignore[union-attr]
    (tmp_path / "state").mkdir()

    backend = JsonCliBackend(m)
    tool = next(t for t in m.tools if t.name == "search")
    out = await backend.call(tool, {"keyword": "年度报告"})
    assert out["exit_code"] == 0
    body = out["result"]
    assert body["stdin"]["version"] == 1
    assert body["stdin"]["operations"][0]["type"] == "search"
    assert body["stdin"]["operations"][0]["keyword"] == "年度报告"
    assert "run" in body["argv"]
    assert "--agent-name" in body["argv"]


def sys_executable() -> str:
    import sys

    return sys.executable


@pytest.mark.anyio
async def test_json_cli_sets_home_from_state_dir(monkeypatch, tmp_path):
    """MCP child must pin HOME=STATE_DIR like runner skill_exec (guangyapan login)."""
    import os

    skill = tmp_path / "skill"
    skill.mkdir()
    recorder = tmp_path / "recorder.py"
    recorder.write_text(
        "\n".join(
            [
                "import json, os, sys",
                "print(json.dumps({\"home\": os.environ.get(\"HOME\"), \"argv\": sys.argv[1:]}))",
            ]
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    m = load_manifest(GUANG)
    m.cli.command = [sys_executable(), str(recorder)]  # type: ignore[union-attr]
    m.cli.cwd = str(skill)  # type: ignore[union-attr]
    m.cli.identity.agent_profile_dir = str(state)  # type: ignore[union-attr]

    monkeypatch.setenv("STATE_DIR", str(state))
    # Poison the process HOME so a missing pin would be visible.
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))

    backend = JsonCliBackend(m)
    tool = next(t for t in m.tools if t.name == "account_rights")
    out = await backend.call(tool, {})
    assert out["exit_code"] == 0
    assert out["result"]["home"] == str(state)
    assert os.environ["HOME"] == str(tmp_path / "wrong-home")
