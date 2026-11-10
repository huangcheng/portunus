"""Runner ``probe`` op with ``argv``: ephemeral tools/list probe (spec §5, §6.2)."""

import json
import os
import sys
import textwrap
import uuid

import pytest
from portunus_runner.main import RunnerServer
from portunus_shared import wire
from test_grace import TOKEN, adopt, connect, wait_until

pytestmark = pytest.mark.anyio

PY = os.path.realpath(sys.executable)

SERVER_SRC = textwrap.dedent(
    """
    import json, os, sys, time
    if os.environ.get("PIDFILE"):
        open(os.environ["PIDFILE"], "w").write(str(os.getpid()))
    mode = os.environ.get("MODE", "ok")
    if mode == "exit":
        sys.exit(3)
    if mode == "noise":
        print("starting up...", flush=True)
    for line in sys.stdin:
        msg = json.loads(line)
        m, i = msg.get("method"), msg.get("id")
        if m == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {
                "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {},
                "serverInfo": {"name": "t", "version": "1"}}}), flush=True)
        elif m == "server/discover":
            print(json.dumps({"jsonrpc": "2.0", "id": i,
                              "error": {"code": -32601, "message": "Method not found"}}), flush=True)
        elif m == "tools/list":
            if mode == "hang":
                time.sleep(3600)
            if mode == "err":
                print(json.dumps({"jsonrpc": "2.0", "id": i,
                                  "error": {"code": -1, "message": "boom"}}), flush=True)
                continue
            if mode == "bad":
                print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {"tools": 5}}), flush=True)
                continue
            if mode == "big":
                t = {"name": "big", "inputSchema": {"type": "object", "d": "x" * 300000}}
                print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {"tools": [t]}}), flush=True)
                continue
            cur = (msg.get("params") or {}).get("cursor")
            tools = [{"name": "a", "inputSchema": {"type": "object"}}]
            res = {"tools": tools}
            if cur is None:
                res["nextCursor"] = "2"
            else:
                res["tools"] = [{"name": "b", "description": "d", "inputSchema": {"type": "object"}}]
            print(json.dumps({"jsonrpc": "2.0", "id": i, "result": res}), flush=True)
    """
)


def _rid():
    return uuid.uuid4().hex


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "srv.py"
    p.write_text(SERVER_SRC)
    return str(p)


@pytest.fixture
def policy_file(tmp_path, script):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"trusted_path": ["/usr/bin", "/bin"], "binary_allowlist": [PY]}))
    return str(p)


@pytest.fixture
async def srv(sock_path, policy_file):
    servers = []

    async def _make(**kw):
        kw.setdefault("heartbeat_s", 5.0)
        kw.setdefault("grace_s", 30.0)
        s = RunnerServer(
            socket_path=sock_path, token=TOKEN, dev=True, policy_path=policy_file, **kw
        )
        await s.start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        await s.close()


def _probe(c, script, *, env=None, timeout_s=None, argv=None, **kw):
    extra = {"timeout_s": timeout_s} if timeout_s is not None else {}
    return {
        "version": 1,
        "op": "probe",
        "request_id": _rid(),
        "token": TOKEN,
        "probe_id": _rid(),
        "server_id": "s",
        "argv": argv or [PY, script],
        "trailing_allowlist": [script],
        "env": env or {},
        **extra,
        **kw,
        **c.ids(),
    }


async def _ready(srv, sock_path):
    s = await srv()
    c = await connect(sock_path)
    assert (await c.hello())["ok"] is True
    return s, c


async def test_probe_lists_tools_across_pages_and_cleans_up(srv, sock_path, script, tmp_path):
    s, c = await _ready(srv, sock_path)
    pidfile = tmp_path / "pid"
    reply = await c.call(_probe(c, script, env={"PIDFILE": str(pidfile)}), timeout=10)
    assert reply["ok"] is True, reply
    assert [t["name"] for t in reply["tools"]] == ["a", "b"]
    assert s.children.empty()  # probe children are outside the child table / inventory
    assert (await c.inventory())["children"] == []
    pid = int(pidfile.read_text())
    assert await wait_until(lambda: not _alive(pid))


async def test_probe_tolerates_stdout_noise(srv, sock_path, script):
    _, c = await _ready(srv, sock_path)
    reply = await c.call(_probe(c, script, env={"MODE": "noise"}), timeout=10)
    assert reply["ok"] is True
    assert len(reply["tools"]) == 2


async def test_probe_timeout_kills_child(srv, sock_path, script, tmp_path):
    _, c = await _ready(srv, sock_path)
    pidfile = tmp_path / "pid"
    reply = await c.call(
        _probe(c, script, env={"MODE": "hang", "PIDFILE": str(pidfile)}, timeout_s=0.5),
        timeout=10,
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "upstream_timeout"
    pid = int(pidfile.read_text())
    assert await wait_until(lambda: not _alive(pid))


async def test_probe_timeout_cannot_be_extended_beyond_default(srv, sock_path, script):
    s, c = await _ready(srv, sock_path)
    # a huge timeout_s is clamped to DEFAULTS.probe_timeout_s (60s); just assert it is accepted
    reply = await c.call(_probe(c, script, timeout_s=10_000), timeout=10)
    assert reply["ok"] is True


@pytest.mark.parametrize(
    "mode,code",
    [("exit", "upstream_crash"), ("err", "upstream_crash"), ("bad", "upstream_crash")],
)
async def test_probe_child_failures(srv, sock_path, script, mode, code):
    _, c = await _ready(srv, sock_path)
    reply = await c.call(_probe(c, script, env={"MODE": mode}), timeout=10)
    assert reply["ok"] is False
    assert reply["error"]["code"] == code


async def test_probe_large_tools_list_over_64k_line(srv, sock_path, script):
    _, c = await _ready(srv, sock_path)
    reply = await c.call(_probe(c, script, env={"MODE": "big"}), timeout=10)
    assert reply["ok"] is True
    assert len(reply["tools"][0]["inputSchema"]["d"]) == 300000


async def test_probe_rejects_bad_argv_at_runner(srv, sock_path, script):
    _, c = await _ready(srv, sock_path)
    for argv in (["/bin/sh", "-c", "id"], ["npx", "demo@latest"], [PY, "/etc/passwd"]):
        reply = await c.call(_probe(c, script, argv=argv))
        assert reply["ok"] is False
        assert reply["error"]["code"] == "spawn_rejected", argv


async def test_probe_pool_capped_at_two(srv, sock_path, script, tmp_path):
    s, c = await _ready(srv, sock_path)
    hang = {"MODE": "hang"}
    first = _probe(c, script, env=hang, timeout_s=30)
    second = _probe(c, script, env=hang, timeout_s=30)
    await c.send(first)
    await c.send(second)
    assert await wait_until(lambda: len(s._probes) == 2)
    third = await c.call(_probe(c, script))
    assert third["ok"] is False
    assert third["error"]["code"] == "child_cap"
    c.close()


async def test_heartbeats_answered_while_probe_runs(srv, sock_path, script):
    _, c = await _ready(srv, sock_path)
    await c.send(_probe(c, script, env={"MODE": "hang"}, timeout_s=2))
    hb = await c.call(wire.heartbeat(request_id=_rid(), token=TOKEN, **c.ids()), timeout=1)
    assert hb["ok"] is True and hb["op"] == "heartbeat"


async def test_session_loss_kills_probe_child(srv, sock_path, script, tmp_path):
    s, c = await _ready(srv, sock_path)
    pidfile = tmp_path / "pid"
    await c.send(_probe(c, script, env={"MODE": "hang", "PIDFILE": str(pidfile)}, timeout_s=30))
    assert await wait_until(lambda: pidfile.exists() and pidfile.read_text() != "")
    pid = int(pidfile.read_text())
    assert _alive(pid)
    c.close()
    assert await wait_until(lambda: not _alive(pid), timeout=5)
    assert await wait_until(lambda: not s._probes)


async def test_shutdown_kills_probe_child(srv, sock_path, script, tmp_path):
    s, c = await _ready(srv, sock_path)
    pidfile = tmp_path / "pid"
    await c.send(_probe(c, script, env={"MODE": "hang", "PIDFILE": str(pidfile)}, timeout_s=30))
    assert await wait_until(lambda: pidfile.exists() and pidfile.read_text() != "")
    pid = int(pidfile.read_text())
    await s.close()
    assert not _alive(pid)


async def test_child_id_liveness_probe_still_works(srv, sock_path):
    s, c = await _ready(srv, sock_path)
    await adopt(s, "c1")
    msg = wire.probe(request_id=_rid(), token=TOKEN, child_id="c1", **c.ids())
    reply = await c.call(msg)
    assert reply["ok"] is True and reply["alive"] is True
    msg = wire.probe(request_id=_rid(), token=TOKEN, child_id="nope", **c.ids())
    reply = await c.call(msg)
    assert reply["ok"] is False and reply["reason"] == "unknown_child"
