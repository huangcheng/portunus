"""Runner argv re-validation + spawn (Task 7). Runner is authoritative at exec."""

import asyncio
import json
import os
import shutil
import stat
import sys
import textwrap
import uuid

import pytest
from portunus_shared import PortunusError, read_frame, wire
from test_grace import TOKEN, connect, script_host, wait_until

from portunus_runner import spawn as spawn_mod
from portunus_runner.main import RunnerServer
from portunus_runner.spawn import Spawner, drop_privs_kwargs

pytestmark = pytest.mark.anyio

SLEEP = os.path.realpath(shutil.which("sleep") or "/bin/sleep")
SH = os.path.realpath("/bin/sh")
PY = script_host()


def _rid():
    return uuid.uuid4().hex


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def env_dir(tmp_path):
    """tmp trusted dir with stub npx/uvx (so rejections are grammar, not 'not found')."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    # stubs record argv/env, then block like a long-lived stdio server
    _exe(bindir / "npx", f'#!/bin/sh\necho "$@" > {out}/npx.args\nenv > {out}/npx.env\nexec /bin/sleep 30\n')
    _exe(bindir / "uvx", f'#!/bin/sh\nenv > {out}/uvx.env\nexec /bin/sleep 30\n')
    return tmp_path


@pytest.fixture
def policy_file(env_dir):
    p = env_dir / "policy.json"
    p.write_text(
        json.dumps(
            {
                "trusted_path": [str(env_dir / "bin"), "/usr/bin", "/bin"],
                "binary_allowlist": [SLEEP, SH, PY],
            }
        )
    )
    return str(p)


@pytest.fixture
async def srv_factory(sock_path, policy_file):
    servers = []

    async def _make(**kw):
        kw.setdefault("heartbeat_s", 5.0)
        kw.setdefault("grace_s", 30.0)
        kw.setdefault("dev", True)
        kw.setdefault("policy_path", policy_file)
        s = RunnerServer(socket_path=sock_path, token=TOKEN, **kw)
        await s.start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        await s.close()


async def _client(srv_factory, sock_path, **kw):
    srv = await srv_factory(**kw)
    c = await connect(sock_path)
    assert (await c.hello())["ok"] is True
    return srv, c


def _start(c, child_id, argv, **kw):
    return wire.start(
        request_id=_rid(), token=TOKEN, child_id=child_id, server_id="s", argv=argv, **c.ids(), **kw
    )


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---- rejection at the runner ------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,trailing",
    [
        (["npx", "--ignore-scripts", "demo@latest"], []),  # dist-tag
        (["npx", "-y", "--ignore-scripts", "demo@latest"], []),
        (["npx", "--ignore-scripts", "demo@^1.0.0"], []),  # range
        (["npx", "--ignore-scripts", "demo"], []),  # unpinned
        (["npx", "demo@1.2.3"], []),  # missing --ignore-scripts
        (["npx", "-c", "echo hi"], []),
        (["npx", "-c", "echo hi"], ["-c", "echo hi"]),  # frame cannot widen the grammar
        (["npx", "--ignore-scripts", "demo@1.2.3", "--eval"], []),  # flag trailing arg
        (["npx", "--ignore-scripts", "git+https://x/y.git"], []),
        (["uvx", "demo"], []),
        (["uvx", "demo>=1.0.0"], []),
        (["sleep", "30"], ["30"]),  # relative, not npx/uvx
        (["/bin/echo", "hi"], ["hi"]),  # not in policy binary_allowlist
        ([SLEEP, "30"], []),  # arg not in trailing_allowlist
        ([SLEEP, "30\x00x"], ["30\x00x"]),  # NUL
    ],
)
async def test_bad_argv_rejected_at_runner(srv_factory, sock_path, argv, trailing):
    srv, c = await _client(srv_factory, sock_path)
    rep = await c.call(_start(c, "k", argv, trailing_allowlist=trailing))
    assert rep["ok"] is False and rep["error"]["code"] == "spawn_rejected", rep
    assert srv.children.empty()
    assert (await c.inventory())["children"] == []
    c.close()


async def test_malformed_argv_and_env_rejected(srv_factory, sock_path):
    srv, c = await _client(srv_factory, sock_path)
    for extra in (
        {"env": {"LD_PRELOAD": "/x.so"}},
        {"env": {"DYLD_INSERT_LIBRARIES": "/x"}},
        {"env": {"PORTUNUS_TOKEN": "x"}},
        {"env": {"BAD NAME": "x"}},
        {"env": {"A": 1}},
        {"env": ["A=1"]},
        {"trailing_allowlist": "30"},
    ):
        frame = _start(c, "k", [SLEEP, "30"], trailing_allowlist=["30"])
        frame.update(extra)
        rep = await c.call(frame)
        assert rep["ok"] is False and rep["error"]["code"] == "spawn_rejected", (extra, rep)
    assert srv.children.empty()
    c.close()


def test_validate_env_allows_skill_bridge_manifest():
    from portunus_shared import SKILL_BRIDGE_MANIFEST_ENV

    env = spawn_mod.validate_env({SKILL_BRIDGE_MANIFEST_ENV: "{}", "WEREAD_API_KEY": "k"})
    assert env[SKILL_BRIDGE_MANIFEST_ENV] == "{}"
    with pytest.raises(PortunusError) as ei:
        spawn_mod.validate_env({"PORTUNUS_TOKEN": "x"})
    assert "not allowed" in ei.value.message


def test_resolve_exec_resolves_python_via_trusted_path(tmp_path):
    from portunus_runner.policy import Policy

    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    policy = Policy(trusted_path=[str(bindir)], binary_allowlist=[])
    assert spawn_mod.resolve_exec("python", policy) == str(py.resolve())


async def test_missing_or_bad_policy_fails_closed(srv_factory, sock_path, env_dir):
    srv, c = await _client(srv_factory, sock_path, policy_path=str(env_dir / "nope.json"))
    rep = await c.call(_start(c, "k", [SLEEP, "30"], trailing_allowlist=["30"]))
    assert rep["ok"] is False and rep["error"]["code"] == "spawn_rejected"
    c.close()
    for body in ("{not json", "[]", json.dumps({"trusted_path": ["/bin"]}),
                 json.dumps({"trusted_path": ["rel"], "binary_allowlist": []}),
                 json.dumps({"trusted_path": ["/bin"], "binary_allowlist": [1]})):
        (env_dir / "bad.json").write_text(body)
        sp = Spawner(policy_path=str(env_dir / "bad.json"), dev=True)
        with pytest.raises(PortunusError) as e:
            await sp({"argv": [SLEEP, "30"], "trailing_allowlist": ["30"]})
        assert e.value.code == "spawn_rejected"


async def test_policy_symlink_refused(env_dir, policy_file):
    link = env_dir / "link.json"
    link.symlink_to(policy_file)
    sp = Spawner(policy_path=str(link), dev=True)
    with pytest.raises(PortunusError) as e:
        await sp({"argv": [SLEEP, "30"], "trailing_allowlist": ["30"]})
    assert e.value.code == "spawn_rejected"


async def test_default_spawn_refused_outside_dev_without_drop_privs(sock_path, policy_file):
    srv = RunnerServer(
        socket_path=sock_path, token=TOKEN, dev=False, expected_uid=os.getuid(),
        heartbeat_s=5.0, grace_s=30.0, policy_path=policy_file,
    )
    await srv.start()
    try:
        c = await connect(sock_path)
        await c.hello()
        rep = await c.call(_start(c, "k", [SLEEP, "30"], trailing_allowlist=["30"]))
        assert rep["ok"] is False and rep["error"]["code"] == "spawn_rejected"
        c.close()
    finally:
        await srv.close()


# ---- successful spawn ----------------------------------------------------------------------


async def test_pinned_binary_starts_and_stop_yields_child_exited(srv_factory, sock_path):
    srv, c = await _client(srv_factory, sock_path)
    rep = await c.call(_start(c, "k1", [SLEEP, "30"], trailing_allowlist=["30"]))
    assert rep["ok"] is True and rep["child_id"] == "k1"
    pid = rep["pid"]
    assert _alive(pid)
    assert [x["child_id"] for x in (await c.inventory())["children"]] == ["k1"]

    probe = await c.call(wire.probe(request_id=_rid(), token=TOKEN, child_id="k1", **c.ids()))
    assert probe["ok"] is True and probe["alive"] is True and probe["pid"] == pid

    rep = await c.call(wire.stop(request_id=_rid(), token=TOKEN, child_id="k1", **c.ids()))
    assert rep["ok"] is True
    note = await asyncio.wait_for(read_frame(c.r), 3.0)
    assert note["op"] == "child_exited" and note["child_id"] == "k1"
    assert note["exit_code"] is not None
    assert not _alive(pid)
    assert (await c.inventory())["children"] == []

    unk = await c.call(wire.probe(request_id=_rid(), token=TOKEN, child_id="k1", **c.ids()))
    assert unk["ok"] is False
    c.close()


async def test_kill_yields_child_exited(srv_factory, sock_path):
    srv, c = await _client(srv_factory, sock_path)
    rep = await c.call(_start(c, "k", [SLEEP, "30"], trailing_allowlist=["30"]))
    pid = rep["pid"]
    assert (await c.call(wire.kill(request_id=_rid(), token=TOKEN, child_id="k", **c.ids())))["ok"]
    note = await asyncio.wait_for(read_frame(c.r), 3.0)
    assert note["op"] == "child_exited" and note["exit_code"] == -9
    assert not _alive(pid)
    c.close()


async def test_stop_escalates_to_sigkill_if_sigterm_ignored(srv_factory, sock_path, env_dir):
    script = _exe(env_dir / "stubborn.sh", "#!/bin/sh\ntrap '' TERM\nwhile :; do /bin/sleep 1; done\n")
    srv, c = await _client(srv_factory, sock_path, stop_kill_s=0.3)
    rep = await c.call(_start(c, "k", [SH, script], trailing_allowlist=[script]))
    assert rep["ok"] is True
    await asyncio.sleep(0.2)  # let sh install the trap
    await c.call(wire.stop(request_id=_rid(), token=TOKEN, child_id="k", **c.ids()))
    note = await asyncio.wait_for(read_frame(c.r), 3.0)
    assert note["op"] == "child_exited" and note["exit_code"] == -9
    c.close()


async def test_pinned_npx_stub_runs_with_literal_env_and_scrubbed_environment(
    srv_factory, sock_path, env_dir, monkeypatch
):
    monkeypatch.setenv("PORTUNUS_TOKEN", "super-secret-runner-token")
    monkeypatch.setenv("SOME_RUNNER_VAR", "leak")
    srv, c = await _client(srv_factory, sock_path)
    rep = await c.call(
        _start(
            c, "n", ["npx", "-y", "--ignore-scripts", "demo@1.2.3", "positional"],
            env={"API_KEY": "lit-123"},
        )
    )
    assert rep["ok"] is True, rep
    out = env_dir / "out"
    assert await wait_until(lambda: (out / "npx.env").exists() and (out / "npx.env").stat().st_size)
    await asyncio.sleep(0.05)
    assert (out / "npx.args").read_text().strip() == "-y --ignore-scripts demo@1.2.3 positional"
    env = dict(l.split("=", 1) for l in (out / "npx.env").read_text().splitlines() if "=" in l)
    assert env["API_KEY"] == "lit-123"
    assert env["PATH"].split(os.pathsep) == [str(env_dir / "bin"), "/usr/bin", "/bin"]
    assert "PORTUNUS_TOKEN" not in env and "SOME_RUNNER_VAR" not in env
    assert "super-secret-runner-token" not in (out / "npx.env").read_text()
    c.close()


async def test_uvx_gets_uv_no_build_not_overridable(srv_factory, sock_path, env_dir):
    srv, c = await _client(srv_factory, sock_path)
    rep = await c.call(_start(c, "u", ["uvx", "demo==1.2.3"], env={"UV_NO_BUILD": "0"}))
    assert rep["ok"] is True, rep
    out = env_dir / "out" / "uvx.env"
    assert await wait_until(lambda: out.exists() and out.stat().st_size)
    await asyncio.sleep(0.05)
    assert "UV_NO_BUILD=1" in out.read_text().splitlines()
    c.close()


# ---- fd hygiene / process hygiene --------------------------------------------------------------


async def test_surplus_fds_closed_in_dev_mode(env_dir, policy_file):
    probe_out = env_dir / "fds.json"
    script = _exe(
        env_dir / "fds.py",
        textwrap.dedent(
            f"""\
            import json, os
            open_fds = []
            for fd in range(3, 256):
                try:
                    os.fstat(fd)
                    open_fds.append(fd)
                except OSError:
                    pass
            json.dump(open_fds, open({str(probe_out)!r}, "w"))
            """
        ),
    )
    r, w = os.pipe()
    os.set_inheritable(r, True)  # would leak without close_fds
    os.set_inheritable(w, True)
    try:
        sp = Spawner(policy_path=policy_file, dev=True)
        h = await sp({"argv": [PY, script], "trailing_allowlist": [script]})
        await asyncio.wait_for(h.wait(), 10)
        assert json.loads(probe_out.read_text()) == []
    finally:
        os.close(r)
        os.close(w)


async def test_grandchildren_swept_when_leader_exits(env_dir, policy_file):
    pidfile = env_dir / "gc.pid"
    script = _exe(env_dir / "gc.sh", f"#!/bin/sh\n/bin/sleep 60 &\necho $! > {pidfile}\nexit 0\n")
    sp = Spawner(policy_path=policy_file, dev=True)
    h = await sp({"argv": [SH, script], "trailing_allowlist": [script]})
    await asyncio.wait_for(h.wait(), 10)
    gc = int(pidfile.read_text())
    deadline = asyncio.get_running_loop().time() + 3
    while _alive(gc) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert not _alive(gc)


async def test_spawn_failure_is_spawn_failed(env_dir, tmp_path):
    missing = tmp_path / "gone"
    pol = tmp_path / "p2.json"
    pol.write_text(json.dumps({"trusted_path": ["/bin"], "binary_allowlist": [str(missing)]}))
    missing.write_text("#!/bin/sh\n")  # exists for allowlist realpath but not executable
    sp = Spawner(policy_path=str(pol), dev=True)
    with pytest.raises(PortunusError) as e:
        await sp({"argv": [str(missing)]})
    assert e.value.code == "spawn_failed"


# ---- production path ---------------------------------------------------------------------------


def test_drop_privs_kwargs_shape(monkeypatch):
    import pwd

    fake = pwd.struct_passwd(("portunus-mcp", "x", 4242, 4343, "", "/nonexistent", "/bin/false"))
    monkeypatch.setattr(spawn_mod.pwd, "getpwnam", lambda n: fake)
    monkeypatch.setattr(spawn_mod, "_make_child_hardener", lambda: (lambda: None))
    kw = drop_privs_kwargs()
    assert kw["user"] == 4242 and kw["group"] == 4343
    assert kw["extra_groups"] == []  # setgroups([])
    assert callable(kw["preexec_fn"])


def test_drop_privs_refuses_root_and_missing_user(monkeypatch):
    import pwd

    root = pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/sh"))
    monkeypatch.setattr(spawn_mod.pwd, "getpwnam", lambda n: root)
    with pytest.raises(PortunusError) as e:
        drop_privs_kwargs()
    assert e.value.code == "spawn_failed"

    def nope(n):
        raise KeyError(n)

    monkeypatch.setattr(spawn_mod.pwd, "getpwnam", nope)
    with pytest.raises(PortunusError) as e:
        drop_privs_kwargs()
    assert e.value.code == "spawn_failed"


async def test_dev_mode_ignores_drop_privs(policy_file):
    assert Spawner(policy_path=policy_file, dev=True, drop_privs=True).drop_privs is False


@pytest.mark.skipif(
    os.environ.get("PORTUNUS_DROP_PRIVS") != "1" or not sys.platform.startswith("linux"),
    reason="needs PORTUNUS_DROP_PRIVS=1, Linux and CAP_SETUID/CAP_SETGID",
)
async def test_production_drop_privs_end_to_end(env_dir, policy_file):
    out = env_dir / "status.txt"
    script = _exe(
        env_dir / "st.py",
        f"import shutil\nshutil.copy('/proc/self/status', {str(out)!r})\n",
    )
    out.touch()
    out.chmod(0o666)
    sp = Spawner(policy_path=policy_file, dev=False, drop_privs=True)
    h = await sp({"argv": [PY, script], "trailing_allowlist": [script]})
    await asyncio.wait_for(h.wait(), 10)
    st = dict(l.split(":\t", 1) for l in out.read_text().splitlines() if ":\t" in l)
    assert st["NoNewPrivs"].strip() == "1"
    for k in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        assert int(st[k], 16) == 0, k
    assert st["Groups"].strip() == ""
    assert int(st["Uid"].split()[0]) != 0
