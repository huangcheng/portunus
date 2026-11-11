import pytest
from portunus_shared.argv import ArgvError, validate_stdio_command

POLICY = {
    "trusted_path": ["/usr/bin", "/bin"],
    "binary_allowlist": [],
}

def test_npx_pinned_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: "/usr/bin/npx")
    validate_stdio_command(
        "npx",
        ["-y", "--ignore-scripts", "demo@1.2.3"],
        trailing_allowlist=[],
        trusted_path=POLICY["trusted_path"],
        binary_allowlist=[],
    )

def test_npx_latest_rejected():
    with pytest.raises(ArgvError) as e:
        validate_stdio_command(
            "npx",
            ["--ignore-scripts", "demo@latest"],
            trailing_allowlist=[],
            trusted_path=["/usr/bin"],
            binary_allowlist=[],
        )
    assert e.value.code == "spawn_rejected"

def test_npx_dash_c_rejected():
    with pytest.raises(ArgvError) as e:
        validate_stdio_command(
            "npx",
            ["-c", "echo hi"],
            trailing_allowlist=[],
            trusted_path=["/usr/bin"],
            binary_allowlist=[],
        )
    assert e.value.code == "spawn_rejected"

def test_uvx_ok(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: "/usr/bin/uvx")
    validate_stdio_command(
        "uvx",
        ["pkg==1.0.0"],
        trailing_allowlist=[],
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )


def test_pikpak_mcp_stdio_ok(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: "/usr/local/bin/pikpak")
    validate_stdio_command(
        "pikpak",
        ["mcp", "--stdio"],
        trailing_allowlist=[],
        trusted_path=["/usr/local/bin"],
        binary_allowlist=[],
    )


def test_pikpak_other_args_rejected(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: "/usr/local/bin/pikpak")
    with pytest.raises(ArgvError) as e:
        validate_stdio_command(
            "pikpak",
            ["mcp"],
            trailing_allowlist=[],
            trusted_path=["/usr/local/bin"],
            binary_allowlist=[],
        )
    assert e.value.code == "spawn_rejected"
