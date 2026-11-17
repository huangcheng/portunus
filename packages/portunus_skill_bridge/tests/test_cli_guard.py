"""cli.command closed grammar + scrubbed child env."""
from __future__ import annotations
import pytest
from portunus_skill_bridge.cli_guard import scrubbed_child_env, validate_cli_command
from portunus_skill_bridge.manifest import Manifest

def test_node_relative_entrypoint_ok():
    validate_cli_command(["node", "scripts/quark-drive.cjs"])

def test_rejects_shell_c():
    with pytest.raises(ValueError, match="shell|eval"):
        validate_cli_command(["/bin/sh", "-c", "echo pwned"])

def test_rejects_node_eval():
    with pytest.raises(ValueError, match="eval"):
        validate_cli_command(["node", "-e", "console.log(1)"])

def test_rejects_node_print_attached():
    with pytest.raises(ValueError, match="eval"):
        validate_cli_command(["node", "-p'1+1'"])

def test_manifest_rejects_shell_command():
    with pytest.raises(Exception):
        Manifest.model_validate({"name":"poc","version":"1.0.0","backend":"json_cli","cli":{"command":["/bin/sh","-c","echo x"],"run_wrapper":"raw"},"tools":[{"name":"noop","cli":["x"]}]})

def test_manifest_rejects_tool_cli_eval_smuggle():
    with pytest.raises(Exception):
        Manifest.model_validate({"name":"poc","version":"1.0.0","backend":"json_cli","cli":{"command":["node"],"run_wrapper":"raw"},"tools":[{"name":"noop","cli":["-e","console.log(1)"]}]})

def test_scrubbed_env_drops_portunus_and_ld(monkeypatch):
    monkeypatch.setenv("PORTUNUS_TOKEN_FILE","/secret/token")
    monkeypatch.setenv("LD_PRELOAD","/evil.so")
    monkeypatch.setenv("CUBOX_TOKEN","ok-secret")
    monkeypatch.setenv("serverUrl","https://example.test")
    monkeypatch.setenv("PATH","/usr/bin")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    env=scrubbed_child_env()
    assert "PORTUNUS_TOKEN_FILE" not in env and "LD_PRELOAD" not in env
    assert env["CUBOX_TOKEN"]=="ok-secret" and env["serverUrl"]=="https://example.test"
    assert env["HOME"] == "/tmp"
    assert env["TMPDIR"] == "/tmp"


def test_scrubbed_env_home_prefers_state_dir(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("STATE_DIR", "/var/lib/portunus/skill-state/cubox")
    env = scrubbed_child_env()
    assert env["HOME"] == "/var/lib/portunus/skill-state/cubox"
