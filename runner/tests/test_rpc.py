"""Runner ``rpc`` op: bridge one ``tools/call`` to a tracked child's stdio (spec §6.2)."""

import json
import os
import sys
import textwrap
import uuid

import pytest
from portunus_runner.main import RunnerServer
from portunus_shared import wire
from test_grace import TOKEN, connect, wait_until

pytestmark = pytest.mark.anyio

PY = os.path.realpath(sys.executable)

SERVER_SRC = textwrap.dedent(
    """
    import json, sys, time
    inited = False
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
        elif m == "notifications/initialized":
            inited = True
        elif m == "tools/call":
            p = msg["params"]
            if not inited:
                print(json.dumps({"jsonrpc": "2.0", "id": i,
                                  "error": {"code": -32002, "message": "not initialized"}}), flush=True)
            elif p["name"] == "sleep":
                time.sleep(3600)
            elif p["name"] == "big":
                text = "x" * (4 * 1024 * 1024 + 10)
                print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {
                    "content": [{"type": "text", "text": text}]}}), flush=True)
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": i, "result": {
                    "content": [{"type": "text", "text": json.dumps(p["arguments"])}]}}), flush=True)
    """
)


def _rid():
    return uuid.uuid4().hex


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "srv.py"
    p.write_text(SERVER_SRC)
    return str(p)


@pytest.fixture
async def ready(sock_path, script, tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"trusted_path": ["/usr/bin", "/bin"], "binary_allowlist": [PY]}))
    s = RunnerServer(
        socket_path=sock_path, token=TOKEN, dev=True, heartbeat_s=5.0, policy_path=str(policy)
    )
    await s.start()
    c = await connect(sock_path)
    assert (await c.hello())["ok"] is True
    rep = await c.call(
        wire.start(
            request_id=_rid(),
            token=TOKEN,
            child_id="c1",
            server_id="s",
            argv=[PY, script],
            trailing_allowlist=[script],
            **c.ids(),
        )
    )
    assert rep["ok"] is True, rep
    yield s, c
    await s.close()


def _rpc(c, name, arguments=None, *, child_id="c1", timeout_s=None, method="tools/call"):
    extra = {"timeout_s": timeout_s} if timeout_s is not None else {}
    return wire.rpc(
        request_id=_rid(),
        token=TOKEN,
        child_id=child_id,
        payload={"method": method, "params": {"name": name, "arguments": arguments or {}}},
        **extra,
        **c.ids(),
    )


async def test_rpc_initializes_lazily_and_returns_result(ready):
    _s, c = ready
    for q in ("a", "b"):  # second call reuses the initialized child
        rep = await c.call(_rpc(c, "echo", {"q": q}), timeout=10)
        assert rep["ok"] is True, rep
        assert json.loads(rep["result"]["content"][0]["text"]) == {"q": q}


async def test_rpc_unknown_child(ready):
    _s, c = ready
    rep = await c.call(_rpc(c, "echo", child_id="nope"))
    assert rep["ok"] is False and rep["reason"] == "unknown_child"


async def test_rpc_only_tools_call_allowed(ready):
    _s, c = ready
    rep = await c.call(_rpc(c, "echo", method="tools/list"))
    assert rep["ok"] is False and rep["error"]["code"] == "unsupported"


async def test_rpc_timeout(ready):
    _s, c = ready
    rep = await c.call(_rpc(c, "sleep", timeout_s=0.5), timeout=10)
    assert rep["ok"] is False and rep["error"]["code"] == "upstream_timeout"


async def test_rpc_oversize_result_is_payload_too_large_and_child_is_killed(ready):
    s, c = ready
    rep = await c.call(_rpc(c, "big"), timeout=20)
    assert rep["ok"] is False and rep["error"]["code"] == "payload_too_large"
    # The child's stdout is desynchronised mid-line: it must not be reused.
    assert await wait_until(s.children.empty, timeout=5)
