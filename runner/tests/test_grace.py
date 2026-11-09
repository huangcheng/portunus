"""Runner session: sole client, heartbeat gap, grace expiry, reconnect disarm, reap-to-empty."""

import asyncio
import os
import signal
import uuid

import pytest
from portunus_shared import encode_frame, read_frame, wire

from portunus_runner.main import RunnerServer

pytestmark = pytest.mark.anyio

TOKEN = "tok-secret"
HB = 0.05  # heartbeat_s override -> gap threshold 3*HB
GRACE = 0.2  # grace_s override


class FakeChild:
    """Injected child handle: kill() makes wait() resolve after `reap_delay`."""

    def __init__(self, pid: int = 4242, reap_delay: float = 0.0) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.reap_delay = reap_delay
        self.killed = False
        self.terminated = False
        self._dead = asyncio.Event()

    def kill(self) -> None:
        self.killed = True

        async def _die():
            await asyncio.sleep(self.reap_delay)
            self.returncode = -signal.SIGKILL
            self._dead.set()

        asyncio.get_running_loop().create_task(_die())

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -signal.SIGTERM
        self._dead.set()

    async def wait(self) -> int:
        await self._dead.wait()
        assert self.returncode is not None
        return self.returncode


class Client:
    def __init__(self, reader, writer, epoch, conn_id):
        self.r, self.w, self.epoch, self.conn_id = reader, writer, epoch, conn_id

    def ids(self):
        return {"gateway_epoch": self.epoch, "conn_id": self.conn_id}

    async def send(self, msg):
        self.w.write(encode_frame(msg))
        await self.w.drain()

    async def call(self, msg, timeout=2.0):
        await self.send(msg)
        return await asyncio.wait_for(read_frame(self.r), timeout)

    async def hello(self, token=TOKEN):
        return await self.call(
            wire.hello(request_id=_rid(), token=token, **self.ids())
        )

    async def inventory(self):
        return await self.call(
            wire.inventory(request_id=_rid(), token=TOKEN, **self.ids())
        )

    async def at_eof(self, timeout=2.0) -> bool:
        try:
            await asyncio.wait_for(self.r.readexactly(1), timeout)
        except asyncio.IncompleteReadError:
            return True
        except (ConnectionResetError, BrokenPipeError):
            return True
        except TimeoutError:
            return False
        return False  # got data

    def close(self):
        self.w.close()


def _rid():
    return uuid.uuid4().hex


async def connect(path, epoch="epoch-A", conn_id=None) -> Client:
    r, w = await asyncio.open_unix_connection(path)
    return Client(r, w, epoch, conn_id or _rid())


async def wait_until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


@pytest.fixture
async def make_server(sock_path):
    servers = []

    async def _make(**kw):
        kw.setdefault("heartbeat_s", HB)
        kw.setdefault("grace_s", GRACE)
        kw.setdefault("dev", True)
        srv = RunnerServer(socket_path=sock_path, token=TOKEN, **kw)
        await srv.start()
        servers.append(srv)
        return srv

    yield _make
    for s in servers:
        await s.close()


async def adopt(srv, child_id="c1", child=None):
    child = child or FakeChild()
    srv.children.add(child_id, child, server_id="s1")
    return child


async def test_grace_expiry_kills_child_and_empties_inventory(make_server, sock_path):
    srv = await make_server()
    c = await connect(sock_path)
    assert (await c.hello())["ok"] is True
    child = await adopt(srv)
    inv = await c.inventory()
    assert [x["child_id"] for x in inv["children"]] == ["c1"]

    # stop sending heartbeats: runner closes the socket (heartbeat gap), then grace expiry
    assert await c.at_eof(timeout=1.0)
    assert await wait_until(srv.children.empty)
    assert child.killed
    assert child.returncode is not None  # reaped

    c.close()
    c2 = await connect(sock_path)
    assert (await c2.hello())["ok"] is True
    assert (await c2.inventory())["children"] == []
    c2.close()


async def test_heartbeat_gap_closes_socket_before_grace_expiry(make_server, sock_path):
    srv = await make_server(grace_s=5.0)
    c = await connect(sock_path)
    await c.hello()
    child = await adopt(srv)
    assert await c.at_eof(timeout=1.0)
    await asyncio.sleep(0.1)
    assert not child.killed  # still within the (long) grace window
    assert not srv.children.empty()
    c.close()


async def test_eof_starts_grace_and_kills(make_server, sock_path):
    srv = await make_server()
    c = await connect(sock_path)
    await c.hello()
    child = await adopt(srv)
    c.close()
    assert await wait_until(srv.children.empty)
    assert child.killed


async def test_heartbeats_keep_connection_alive(make_server, sock_path):
    srv = await make_server()
    c = await connect(sock_path)
    await c.hello()
    child = await adopt(srv)
    for _ in range(12):  # ~0.3s > gap (0.15s) and > grace
        ack = await c.call(wire.heartbeat(request_id=_rid(), token=TOKEN, **c.ids()))
        assert ack["ok"] is True
        await asyncio.sleep(0.025)
    assert not child.killed
    c.close()


async def test_same_epoch_reconnect_disarms_grace(make_server, sock_path):
    srv = await make_server(grace_s=0.5)
    c = await connect(sock_path, epoch="E1")
    await c.hello()
    child = await adopt(srv)
    c.close()
    await asyncio.sleep(0.1)  # runner has seen EOF and armed grace
    assert srv.grace_armed
    c2 = await connect(sock_path, epoch="E1")
    assert (await c2.hello())["ok"] is True
    assert not srv.grace_armed
    inv = await c2.inventory()
    assert [x["child_id"] for x in inv["children"]] == ["c1"]
    # keep alive past the original grace deadline
    for _ in range(25):
        await c2.call(wire.heartbeat(request_id=_rid(), token=TOKEN, **c2.ids()))
        await asyncio.sleep(0.03)
    assert not child.killed
    c2.close()


async def test_new_epoch_hello_kills_and_reaps_before_empty_inventory(make_server, sock_path):
    srv = await make_server(grace_s=30.0)
    c = await connect(sock_path, epoch="E1")
    await c.hello()
    child = await adopt(srv, child=FakeChild(reap_delay=0.15))
    c.close()
    await asyncio.sleep(0.1)
    c2 = await connect(sock_path, epoch="E2")
    rep = await c2.hello()
    # the hello reply may only arrive once every child has been reaped
    assert rep["ok"] is True
    assert child.killed and child.returncode is not None
    assert srv.children.empty()
    assert (await c2.inventory())["children"] == []
    c2.close()


async def test_second_concurrent_connection_rejected(make_server, sock_path):
    await make_server(grace_s=30.0, heartbeat_s=5.0)
    c1 = await connect(sock_path)
    await c1.hello()
    c2 = await connect(sock_path)
    assert await c2.at_eof(timeout=1.0)
    # first client unaffected
    ack = await c1.call(wire.heartbeat(request_id=_rid(), token=TOKEN, **c1.ids()))
    assert ack["ok"] is True
    c1.close()
    c2.close()
    # after the first disconnects, the slot frees and a new client is accepted
    await asyncio.sleep(0.1)
    c3 = await connect(sock_path)
    assert (await c3.hello())["ok"] is True
    c3.close()


async def test_bad_token_rejected_and_does_not_disarm_grace(make_server, sock_path):
    srv = await make_server(grace_s=0.4)
    c = await connect(sock_path, epoch="E1")
    await c.hello()
    child = await adopt(srv)
    c.close()
    await asyncio.sleep(0.1)
    assert srv.grace_armed
    bad = await connect(sock_path, epoch="E1")
    rep = await bad.hello(token="wrong")
    assert rep["ok"] is False and rep["error"]["code"] == "unauthorized"
    assert "wrong" not in str(rep) and TOKEN not in str(rep)
    assert await bad.at_eof(timeout=1.0)
    bad.close()
    assert srv.grace_armed
    assert await wait_until(srv.children.empty)
    assert child.killed


async def test_bad_token_after_hello_closes_session(make_server, sock_path):
    await make_server(grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    await c.hello()
    rep = await c.call(wire.heartbeat(request_id=_rid(), token="nope", **c.ids()))
    assert rep["ok"] is False and rep["error"]["code"] == "unauthorized"
    assert await c.at_eof(timeout=1.0)
    c.close()


async def test_must_hello_first(make_server, sock_path):
    await make_server()
    c = await connect(sock_path)
    rep = await c.call(wire.inventory(request_id=_rid(), token=TOKEN, **c.ids()))
    assert rep["ok"] is False
    assert await c.at_eof(timeout=1.0)
    c.close()


async def test_superseded_conn_id_frames_dropped(make_server, sock_path):
    await make_server(grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    await c.hello()
    await c.send(
        wire.inventory(request_id="stale", token=TOKEN, gateway_epoch=c.epoch, conn_id="old-conn")
    )
    # stale frame gets no reply; the next valid one does
    rep = await c.inventory()
    assert rep["op"] == "inventory" and rep["request_id"] != "stale"
    c.close()


async def test_peer_uid_enforced_outside_dev(make_server, sock_path):
    me = os.getuid()
    await make_server(dev=False, expected_uid=me + 12345, grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    assert await c.at_eof(timeout=1.0)  # wrong uid -> closed before any frame
    c.close()


async def test_peer_uid_match_accepted_outside_dev(make_server, sock_path):
    await make_server(dev=False, expected_uid=os.getuid(), grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    assert (await c.hello())["ok"] is True
    c.close()


async def test_dev_skips_uid_but_still_requires_token(make_server, sock_path):
    await make_server(dev=True, expected_uid=os.getuid() + 1, grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    rep = await c.hello(token="bad")
    assert rep["ok"] is False
    c.close()
    c2 = await connect(sock_path)
    assert (await c2.hello())["ok"] is True
    c2.close()


async def test_non_dev_requires_expected_uid(sock_path):
    with pytest.raises(ValueError):
        RunnerServer(socket_path=sock_path, token=TOKEN, dev=False)


async def test_start_stop_kill_via_injected_spawn(make_server, sock_path):
    spawned: dict[str, FakeChild] = {}

    async def spawn(msg):
        ch = FakeChild(pid=1000 + len(spawned))
        spawned[msg["child_id"]] = ch
        return ch

    await make_server(spawn=spawn, grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    await c.hello()
    rep = await c.call(
        wire.start(
            request_id=_rid(), token=TOKEN, child_id="k1", server_id="s", argv=["x"], **c.ids()
        )
    )
    assert rep["ok"] is True and rep["child_id"] == "k1"
    dup = await c.call(
        wire.start(
            request_id=_rid(), token=TOKEN, child_id="k1", server_id="s", argv=["x"], **c.ids()
        )
    )
    assert dup["ok"] is False
    inv = await c.inventory()
    assert [x["child_id"] for x in inv["children"]] == ["k1"]
    rep = await c.call(wire.stop(request_id=_rid(), token=TOKEN, child_id="k1", **c.ids()))
    assert rep["ok"] is True and spawned["k1"].terminated
    # child_exited async notification then empty inventory
    note = await asyncio.wait_for(read_frame(c.r), 2.0)
    assert note["op"] == "child_exited" and note["child_id"] == "k1"
    assert (await c.inventory())["children"] == []
    unk = await c.call(wire.kill(request_id=_rid(), token=TOKEN, child_id="nope", **c.ids()))
    assert unk["ok"] is False
    c.close()


async def test_default_spawn_refused_outside_dev(sock_path):
    srv = RunnerServer(
        socket_path=sock_path, token=TOKEN, dev=False, expected_uid=os.getuid(),
        heartbeat_s=5.0, grace_s=30.0,
    )
    await srv.start()
    try:
        c = await connect(sock_path)
        await c.hello()
        rep = await c.call(
            wire.start(
                request_id=_rid(), token=TOKEN, child_id="k", server_id="s",
                argv=["/bin/sleep", "30"], **c.ids(),
            )
        )
        assert rep["ok"] is False and rep["error"]["code"] == "spawn_rejected"
        c.close()
    finally:
        await srv.close()


async def test_real_process_killed_and_reaped_on_grace_expiry(make_server, sock_path, tmp_path):
    """Integration with a real child (dev mode, policy-allowlisted absolute /bin/sleep)."""
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"trusted_path": ["/usr/bin", "/bin"], "binary_allowlist": ["/bin/sleep"]}'
    )
    srv = await make_server(policy_path=str(policy))
    c = await connect(sock_path)
    await c.hello()
    rep = await c.call(
        wire.start(
            request_id=_rid(), token=TOKEN, child_id="real", server_id="s",
            argv=["/bin/sleep", "30"], trailing_allowlist=["30"], **c.ids(),
        )
    )
    assert rep["ok"] is True
    pid = rep["pid"]
    os.kill(pid, 0)  # alive
    # no heartbeats: gap -> close -> grace -> SIGKILL + reap
    assert await c.at_eof(timeout=1.0)
    assert await wait_until(srv.children.empty, timeout=3.0)

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    assert gone()  # reaped, not a zombie (kill(pid,0) succeeds on zombies)
    c.close()
    c2 = await connect(sock_path)
    await c2.hello()
    assert (await c2.inventory())["children"] == []
    c2.close()


@pytest.mark.parametrize(
    "frame",
    [
        {"version": 2, "op": "hello"},  # bad version, no token
        {"version": 1, "op": "nope", "token": "wrong"},  # bad op
        {"version": 1, "op": "hello", "request_id": "r"},  # no token
        {"version": 1, "op": "hello", "request_id": "r", "token": "wrong"},
        {"version": 1, "op": "inventory", "request_id": "r", "token": TOKEN},  # no hello
        {"version": 1, "op": "hello", "request_id": "r", "token": TOKEN},  # no epoch/conn_id
    ],
)
async def test_no_token_in_any_reply_before_authentication(make_server, sock_path, frame):
    await make_server(grace_s=30.0, heartbeat_s=5.0)
    c = await connect(sock_path)
    rep = await c.call(frame)
    assert rep["ok"] is False
    assert TOKEN not in str(rep) and "token" not in rep
    c.close()


async def test_no_token_in_reply_to_garbage_frame(make_server, sock_path):
    await make_server(grace_s=30.0, heartbeat_s=5.0)
    r, w = await asyncio.open_unix_connection(sock_path)
    w.write((5).to_bytes(4, "big") + b"[1,2]")  # valid length, JSON but not an object
    await w.drain()
    rep = await asyncio.wait_for(read_frame(r), 2.0)
    assert rep["ok"] is False and "token" not in rep and TOKEN not in str(rep)
    w.close()
