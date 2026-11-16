import os

import pytest
from portunus_shared import DEFAULTS, ERROR_CODES
from portunus_shared.argv import ArgvError, validate_stdio_command

NPX = "portunus_shared.argv.which"


def _v(command, args, *, trailing=(), path=("/usr/bin",), binaries=()):
    validate_stdio_command(
        command,
        list(args),
        trailing_allowlist=list(trailing),
        trusted_path=list(path),
        binary_allowlist=list(binaries),
    )


@pytest.fixture
def with_which(monkeypatch):
    monkeypatch.setattr(NPX, lambda c, path=None: f"/usr/bin/{c}")


@pytest.mark.parametrize(
    "args",
    [
        ["--ignore-scripts", "demo@1.2.3"],
        ["--yes", "--ignore-scripts", "@scope/demo@10.20.30"],
        ["-y", "--ignore-scripts", "demo@1.2.3", "positional"],
    ],
)
def test_npx_ok(with_which, args):
    _v("npx", args)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["demo@1.2.3"],  # missing --ignore-scripts
        ["--ignore-scripts"],  # missing package
        ["--ignore-scripts", "demo"],
        ["--ignore-scripts", "demo@^1.2.3"],
        ["--ignore-scripts", "demo@1.2"],
        ["--ignore-scripts", "demo@1.2.3-beta"],
        ["--ignore-scripts", "git+https://x/y.git"],
        ["--ignore-scripts", "file:../x"],
        ["--ignore-scripts", "https://x/y.tgz"],
        ["--ignore-scripts", "demo@1.2.3", "--evil"],
        ["--ignore-scripts", "a@1.2.3", "b@1.2.3", "-x"],
        ["--ignore-scripts", "-y", "demo@1.2.3"],  # -y must precede
        ["-y", "-y", "--ignore-scripts", "demo@1.2.3"],
    ],
)
def test_npx_rejected(with_which, args):
    with pytest.raises(ArgvError) as e:
        _v("npx", args)
    assert e.value.code == "spawn_rejected"


def test_npx_trailing_flag_allowlisted(with_which):
    _v("npx", ["--ignore-scripts", "demo@1.2.3", "--port"], trailing=["--port"])


@pytest.mark.parametrize(
    "args",
    [[], ["pkg"], ["pkg>=1.0.0"], ["pkg==1.0"], ["pkg==1.0.0", "-x"], ["--from", "pkg==1.0.0"]],
)
def test_uvx_rejected(with_which, args):
    with pytest.raises(ArgvError):
        _v("uvx", args)


def test_uvx_trailing(with_which):
    _v("uvx", ["pkg==1.0.0", "serve"])
    _v("uvx", ["pkg==1.0.0", "--flag"], trailing=["--flag"])


def test_uvx_with_pins(with_which):
    _v("uvx", ["--with", "mcp==1.1.2", "mcp-server-cvm==0.1.12"])
    _v("uvx", ["--with", "a==1.0.0", "--with", "b==2.0.0", "tool==3.0.0"])
    with pytest.raises(ArgvError):
        _v("uvx", ["--with", "mcp", "tool==1.0.0"])
    with pytest.raises(ArgvError):
        _v("uvx", ["--with", "mcp==1.1.2"])


def test_basename_not_found_in_trusted_path(monkeypatch):
    monkeypatch.setattr(NPX, lambda c, path=None: None)
    with pytest.raises(ArgvError):
        _v("uvx", ["pkg==1.0.0"])


def test_which_receives_trusted_path(monkeypatch):
    seen = {}

    def fake(c, path=None):
        seen["path"] = path
        return "/usr/bin/uvx"

    monkeypatch.setattr(NPX, fake)
    _v("uvx", ["pkg==1.0.0"], path=["/a", "/b"])
    assert seen["path"] == os.pathsep.join(["/a", "/b"])


def test_absolute_allowlisted(tmp_path):
    exe = tmp_path / "srv"
    exe.write_text("#!/bin/sh\n")
    _v(str(exe), [], binaries=[str(exe)])
    _v(str(exe), ["--ok"], trailing=["--ok"], binaries=[str(exe)])


def test_absolute_symlink_realpath(tmp_path):
    real = tmp_path / "real"
    real.write_text("")
    link = tmp_path / "link"
    link.symlink_to(real)
    _v(str(link), [], binaries=[str(real)])
    with pytest.raises(ArgvError):
        _v(str(link), [], binaries=[str(link.parent / "other")])


def test_absolute_hardened_basenames_keep_grammar(tmp_path):
    npx = tmp_path / "npx"
    npx.write_text("#!/bin/sh\n")
    py = tmp_path / "python3.12"
    py.write_text("")
    with pytest.raises(ArgvError):
        _v(str(npx), ["demo@1.2.3"], binaries=[str(npx)])
    _v(str(npx), ["--ignore-scripts", "demo@1.2.3"], binaries=[str(npx)])
    with pytest.raises(ArgvError):
        _v(str(py), ["script.py"], trailing=["script.py"], binaries=[str(py)])
    _v(str(py), ["-m", "portunus_skill_bridge", "--manifest-stdin"], binaries=[str(py)])


def test_absolute_rejected(tmp_path):
    exe = tmp_path / "srv"
    exe.write_text("")
    with pytest.raises(ArgvError):
        _v(str(exe), [], binaries=[])
    with pytest.raises(ArgvError):
        _v(str(exe), ["extra"], binaries=[str(exe)])


@pytest.mark.parametrize("cmd", ["bash", "sh", "node", "./npx", "npx/../sh", ""])
def test_other_commands_rejected(with_which, cmd):
    with pytest.raises(ArgvError):
        _v(cmd, [])


def test_python_skill_bridge_ok(with_which):
    _v("python", ["-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"])
    _v("python3", ["-m", "portunus_skill_bridge", "--manifest-stdin"])


def test_python_skill_bridge_rejects(with_which):
    with pytest.raises(ArgvError):
        _v("python", ["-m", "other_mod", "--manifest-env", "X"])
    with pytest.raises(ArgvError):
        _v("python", ["-m", "portunus_skill_bridge"])
    with pytest.raises(ArgvError):
        _v("python", ["script.py"])


def test_error_codes_match_spec_6_4_exactly():
    assert ERROR_CODES == frozenset(
        {
            "unauthorized",
            "revoked",
            "not_granted",
            "server_disabled",
            "tool_disabled",
            "tool_missing",
            "schema_mismatch",
            "schema_drift",
            "invalid_tool_arguments",
            "invalid_gateway_arguments",
            "invalid_cursor",
            "payload_too_large",
            "upstream_timeout",
            "upstream_crash",
            "upstream_error",
            "cancelled",
            "auth_required",
            "child_cap",
            "rate_limited",
            "runner_unavailable",
            "spawn_rejected",
            "spawn_failed",
            "unsupported",
        }
    )


def test_argv_error_code_is_always_spawn_rejected():
    assert ArgvError("x").code == "spawn_rejected"
    with pytest.raises(TypeError):
        ArgvError("x", code="revoked")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ArgvError("x", "revoked")  # type: ignore[call-arg]


def test_defaults():
    assert (
        DEFAULTS.invoke_timeout_s,
        DEFAULTS.start_ack_s,
        DEFAULTS.grace_s,
        DEFAULTS.heartbeat_s,
        DEFAULTS.max_children_per_key,
        DEFAULTS.max_children_global,
        DEFAULTS.max_probes,
        DEFAULTS.probe_timeout_s,
    ) == (60, 15, 30, 10, 32, 256, 2, 60)
