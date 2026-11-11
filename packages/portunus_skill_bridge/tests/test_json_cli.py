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
