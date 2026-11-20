import json
from pathlib import Path

import pytest

from portunus_skill_bridge.backends.json_cli import JsonCliBackend
from portunus_skill_bridge.manifest import load_manifest

GUANG = Path(__file__).resolve().parents[1] / "manifests" / "guangyapan.example.yaml"
CUBOX = Path(__file__).resolve().parents[1] / "manifests" / "cubox.example.yaml"


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


def test_absolute_state_home_creates_under_skill_state(tmp_path, monkeypatch):
    """First himalaya tool call must mkdir catalog STATE_DIR leaves."""
    from portunus_skill_bridge.backends import json_cli as jc

    root = tmp_path / "skill-state"
    root.mkdir()
    monkeypatch.setattr(jc, "_SKILL_STATE_PREFIX", str(root) + "/")
    leaf = root / "vivaldi-mail"
    assert not leaf.exists()
    got = jc._absolute_state_home(str(leaf), label="STATE_DIR")
    assert got == str(leaf)
    assert leaf.is_dir()


def test_absolute_state_home_refuses_outside_prefix(tmp_path):
    from portunus_skill_bridge.backends import json_cli as jc

    outside = tmp_path / "other"
    try:
        jc._absolute_state_home(str(outside), label="STATE_DIR")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "absolute existing directory" in str(e)
    assert not outside.exists()


def test_absolute_state_home_refuses_traversal_escape(tmp_path, monkeypatch):
    from portunus_skill_bridge.backends import json_cli as jc

    root = tmp_path / "skill-state"
    root.mkdir()
    monkeypatch.setattr(jc, "_SKILL_STATE_PREFIX", str(root) + "/")
    # Un-normalized string starts with the prefix but resolves outside it.
    escape = str(root / ".." / "outside-leaf")
    try:
        jc._absolute_state_home(escape, label="STATE_DIR")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "absolute existing directory" in str(e)
    assert not (tmp_path / "outside-leaf").exists()


@pytest.mark.anyio
async def test_json_cli_pins_home_when_unset(monkeypatch, tmp_path):
    """Cubox-style: scrubbed MCP env has no HOME/STATE_DIR — still pin a home."""
    import os

    recorder = tmp_path / "recorder.py"
    recorder.write_text(
        "\n".join(
            [
                "import json, os, sys",
                "print(json.dumps({\"home\": os.environ.get(\"HOME\")}))",
            ]
        ),
        encoding="utf-8",
    )
    m = load_manifest(CUBOX)
    m.cli.command = [sys_executable(), str(recorder)]  # type: ignore[union-attr]

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)

    backend = JsonCliBackend(m)
    tool = next(t for t in m.tools if t.name == "tag_list")
    out = await backend.call(tool, {})
    assert out["exit_code"] == 0
    assert out["result"]["home"] == "/tmp"
    assert "HOME" not in os.environ


@pytest.mark.anyio
async def test_json_cli_multi_token_cli_prefix(tmp_path):
    """Cubox-style tools pass cli as ['folder','list'] before flags."""
    recorder = tmp_path / "recorder.py"
    recorder.write_text(
        "\n".join(
            [
                "import json,sys",
                "print(json.dumps({\"argv\": sys.argv[1:]}))",
            ]
        ),
        encoding="utf-8",
    )
    m = load_manifest(CUBOX)
    m.cli.command = [sys_executable(), str(recorder)]  # type: ignore[union-attr]
    backend = JsonCliBackend(m)
    tool = next(t for t in m.tools if t.name == "card_list")
    out = await backend.call(tool, {"keyword": "AI", "page": 1})
    assert out["exit_code"] == 0
    argv = out["result"]["argv"]
    assert argv[:2] == ["card", "list"]
    assert "--keyword" in argv and "AI" in argv
    assert "--page" in argv and "1" in argv
