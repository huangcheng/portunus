"""RunnerClient against a minimal in-test runner speaking the real wire codec."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import uuid

import pytest
from portunus_shared import PortunusError, encode_frame, read_frame

from portunus_gateway.runner_client import RunnerClient, RunnerRequestError
from portunus_gateway.slots import SlotManager

pytestmark = pytest.mark.anyio

TOKEN = "tok-123"


@pytest.fixture
def sock_path():
    d = tempfile.mkdtemp(prefix="pgw-", dir="/tmp")  # AF_UNIX paths are short on macOS
    try:
        yield f"{d}/r.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


class FakeRunnerServer:
    def __init__(
        self,
        path: str,
        *,
        token: str = TOKEN,
        hello_ok: bool = True,
        hello_fields: dict | None = None,
    ) -> None:
        self.path = path
        self.hello_fields = hello_fields or {}  # extra fleet metadata on the hello reply
        self.token = token
        self.hello_ok = hello_ok
        self.frames: list[dict] = []  # post-hello frames received
        self.hellos: list[dict] = []
        self.current: asyncio.StreamWriter | None = None
        self.respond = True
        self.heartbeat_replies = True
        self.children: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)

    async def close(self) -> None:
        if self._server:
            self._server.close()
        if self.current:
            self.current.close()
        for t in list(self._tasks):
            t.cancel()

    def reply(self, msg: dict, **fields) -> dict:
        return {
            "version": 1,
            "op": msg["op"],
            "request_id": msg["request_id"],
            "token": self.token,
            "ok": True,
            **fields,
        }

    async def send(self, frame: dict) -> None:
        assert self.current is not None
        self.current.write(encode_frame(frame))
        await self.current.drain()

    async def _handle(self, reader, writer) -> None:
        self._tasks.add(asyncio.current_task())
        self.current = writer
        try:
            hello = await read_frame(reader)
            self.hellos.append(hello)
            if not self.hello_ok:
                writer.close()
                return
            await self.send(
                self.reply(hello, run_id="run-1", wire_version=1, **self.hello_fields)
            )
            while True:
                msg = await read_frame(reader)
                self.frames.append(msg)
                await self._on_frame(msg)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            if self.current is writer:
                self.current = None

    async def _on_frame(self, msg: dict) -> None:
        op = msg["op"]
        if op == "heartbeat":
            if self.heartbeat_replies:
                await self.send(self.reply(msg))
        elif not self.respond:
            return
        elif op == "inventory":
            await self.send(self.reply(msg, run_id="run-1", children=self.children))
        elif op == "start":
            self.children.append(
                {"child_id": msg["child_id"], "server_id": msg["server_id"], "pid": 7}
            )
            await self.send(self.reply(msg, child_id=msg["child_id"], pid=7))
        elif op == "kill":
            await self.send(
                {
                    "version": 1,
                    "op": "kill",
                    "request_id": msg["request_id"],
                    "token": self.token,
                    "ok": False,
                    "error": {"code": "unsupported", "message": "unknown child"},
                    "reason": "unknown_child",
                }
            )
        else:
            await self.send(self.reply(msg))


async def _wait(cond, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


def make_client(path, **kw) -> RunnerClient:
    kw.setdefault("retry_initial_s", 0.02)
    kw.setdefault("retry_max_s", 0.05)
    return RunnerClient(path, TOKEN, **kw)


@contextlib.asynccontextmanager
async def running(client: RunnerClient):
    client.start()
    try:
        yield client
    finally:
        await client.close()


async def test_hello_and_request_stamp_epoch_and_conn_id(sock_path):
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    async with running(make_client(sock_path, epoch="E1")) as c:
        await c.wait_connected(2)
        assert c.conn_seq == 1 and c.run_id == "run-1"
        h = srv.hellos[0]
        assert h["gateway_epoch"] == "E1" and h["conn_id"] and h["token"] == TOKEN
        inv = await c.inventory()
        assert inv["children"] == []
        f = srv.frames[-1]
        assert (f["gateway_epoch"], f["conn_id"], f["token"]) == ("E1", h["conn_id"], TOKEN)
    await srv.close()


async def test_retries_connect_until_runner_appears(sock_path):
    async with running(make_client(sock_path)) as c:
        await asyncio.sleep(0.1)
        assert not c.connected
        srv = FakeRunnerServer(sock_path)
        await srv.start()
        await c.wait_connected(2)
        await srv.close()


async def test_error_reply_maps_to_error_with_reason(sock_path):
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    async with running(make_client(sock_path)) as c:
        await c.wait_connected(2)
        with pytest.raises(RunnerRequestError) as ei:
            await c.kill_child("nope")
        assert ei.value.code == "unsupported" and ei.value.reason == "unknown_child"
        assert ei.value.reply is not None
    await srv.close()


async def test_request_when_disconnected_is_not_sent(sock_path):
    c = make_client(sock_path)
    with pytest.raises(RunnerRequestError) as ei:
        await c.request("inventory", timeout=1)
    assert ei.value.code == "runner_unavailable" and ei.value.sent is False


async def test_request_timeout(sock_path):
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    async with running(make_client(sock_path)) as c:
        await c.wait_connected(2)
        srv.respond = False
        with pytest.raises(RunnerRequestError) as ei:
            await c.request("inventory", timeout=0.1)
        assert ei.value.code == "upstream_timeout" and ei.value.sent is True
    await srv.close()


async def test_disconnect_fails_inflight_then_reconnects_same_epoch(sock_path):
    connected: list[int] = []
    disconnected: list[bool] = []

    async def on_connected():
        connected.append(c.conn_seq)

    srv = FakeRunnerServer(sock_path)
    await srv.start()
    c = make_client(
        sock_path, epoch="E1", on_connected=on_connected, on_disconnected=lambda: disconnected.append(True)
    )
    async with running(c):
        await c.wait_connected(2)
        srv.respond = False
        pending = asyncio.create_task(c.request("inventory", timeout=5))
        await _wait(lambda: any(f["op"] == "inventory" for f in srv.frames))
        srv.current.close()
        with pytest.raises(RunnerRequestError) as ei:
            await pending
        assert ei.value.code == "runner_unavailable" and ei.value.sent is True
        await _wait(lambda: c.conn_seq == 2 and c.connected)
        assert connected == [1, 2] and disconnected == [True]
        assert [h["gateway_epoch"] for h in srv.hellos] == ["E1", "E1"]
        assert srv.hellos[0]["conn_id"] != srv.hellos[1]["conn_id"]
    await srv.close()


async def test_child_exited_delivered_and_superseded_dropped(sock_path):
    got: list[str] = []
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    c = make_client(sock_path, epoch="E1", on_child_exited=got.append)
    async with running(c):
        await c.wait_connected(2)
        conn_id = srv.hellos[0]["conn_id"]

        def exited(cid, **kw):
            return {
                "version": 1, "op": "child_exited", "request_id": uuid.uuid4().hex,
                "token": TOKEN, "child_id": cid, "exit_code": 0,
                "gateway_epoch": "E1", "conn_id": conn_id, **kw,
            }

        await srv.send(exited("stale", conn_id="old-conn"))
        await srv.send(exited("wrong-epoch", gateway_epoch="E0"))
        await srv.send(exited("c1"))
        await _wait(lambda: got)
        assert got == ["c1"]
    await srv.close()


async def test_bad_hello_token_not_connected(sock_path):
    srv = FakeRunnerServer(sock_path, token="other")
    await srv.start()
    async with running(make_client(sock_path)) as c:
        await asyncio.sleep(0.2)
        assert not c.connected and len(srv.hellos) >= 2  # keeps retrying
    await srv.close()


async def test_missing_heartbeat_reply_drops_connection(sock_path):
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    async with running(make_client(sock_path, heartbeat_s=0.05)) as c:
        await c.wait_connected(2)
        srv.heartbeat_replies = False
        await _wait(lambda: c.conn_seq >= 1 and not c.connected, timeout=2)
        srv.heartbeat_replies = True
        await _wait(lambda: c.connected and c.conn_seq >= 2)
    await srv.close()


async def test_on_connected_failure_drops_and_retries(sock_path):
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("inventory failed")

    srv = FakeRunnerServer(sock_path)
    await srv.start()
    async with running(make_client(sock_path, on_connected=flaky)) as c:
        await _wait(lambda: calls >= 2)
        await _wait(lambda: c.connected)
    await srv.close()


async def test_expected_uid_enforced(sock_path):
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    async with running(make_client(sock_path, expected_uid=os.getuid() + 12345)) as c:
        await asyncio.sleep(0.2)
        assert not c.connected
    async with running(make_client(sock_path, expected_uid=os.getuid())) as c:
        await c.wait_connected(2)
    await srv.close()


async def test_slot_manager_over_real_client(sock_path, authz):
    """End to end over the socket: explicit start frame, child_exited frees, reconcile on connect."""
    srv = FakeRunnerServer(sock_path)
    await srv.start()
    c = make_client(sock_path)
    sm = SlotManager(c, authz, stop_retry_s=0.05)
    sm.attach(c)
    key, server = "k1", uuid.uuid4()
    authz._grants.add((key, server))
    authz.bump()
    async with running(c):
        await c.wait_connected(2)
        await _wait(lambda: any(f["op"] == "inventory" for f in srv.frames))
        cid = await sm.reserve_and_start(
            server, key, {"argv": ["uvx", "pkg==1.0.0"], "env": {"A": "b"}, "vault_version": 3}
        )
        f = next(x for x in srv.frames if x["op"] == "start")
        assert f["child_id"] == cid and f["argv"] == ["uvx", "pkg==1.0.0"]
        assert f["env"] == {"A": "b"} and f["server_id"] == str(server)
        assert f["trailing_allowlist"] == [] and f["gateway_epoch"] == c.epoch
        assert sm.slot(cid) is not None
        await srv.send(
            {
                "version": 1, "op": "child_exited", "request_id": "x", "token": TOKEN,
                "child_id": cid, "exit_code": 1,
                "gateway_epoch": c.epoch, "conn_id": srv.hellos[-1]["conn_id"],
            }
        )
        await _wait(lambda: sm.slots == {})
        # a runner-side refusal propagates as its code
        srv.children.clear()
        # reconnect: runner now lists a child nobody authorised -> killed (unknown_child -> freed)
        srv.children.append({"child_id": "ghost", "server_id": str(server), "pid": 1})
        srv.current.close()
        await _wait(lambda: any(f["op"] == "kill" and f["child_id"] == "ghost" for f in srv.frames))
        await _wait(lambda: sm.slots == {})
    await srv.close()


async def test_slot_manager_runner_down(sock_path, authz):
    c = make_client(sock_path)
    sm = SlotManager(c, authz)
    authz._grants.add(("k", uuid.UUID(int=1)))
    with pytest.raises(PortunusError) as ei:
        await sm.reserve_and_start(uuid.UUID(int=1), "k", {"argv": ["npx"]})
    assert ei.value.code == "runner_unavailable"


async def test_hello_records_site_and_regions(sock_path):
    srv = FakeRunnerServer(
        sock_path,
        hello_fields={
            "site_id": "site-a",
            "regions": ["region-a", "region-b"],
            "egress_class": "standard",
            "capacity": 8,
        },
    )
    await srv.start()
    async with running(make_client(sock_path)) as c:
        await c.wait_connected(2)
        assert c.site_id == "site-a"
        assert c.regions == ["region-a", "region-b"]
        assert c.egress_class == "standard"
        assert c.capacity == 8
    await srv.close()


async def test_v1_runner_without_fleet_fields_gets_defaults(sock_path):
    srv = FakeRunnerServer(sock_path)  # hello reply carries no fleet fields
    await srv.start()
    async with running(make_client(sock_path)) as c:
        assert (c.site_id, c.regions) == ("local", ["local"])  # defaults before connect too
        await c.wait_connected(2)
        assert c.site_id == "local"
        assert c.regions == ["local"]
        assert c.egress_class is None and c.capacity is None
        assert (await c.inventory())["children"] == []
    await srv.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"site_id": "", "regions": []},
        {"site_id": 7, "regions": "region-a"},
        {"site_id": None, "regions": [1, ""], "capacity": True, "egress_class": 5},
        {"capacity": -1},
    ],
)
async def test_malformed_fleet_fields_fall_back_to_defaults(sock_path, fields):
    srv = FakeRunnerServer(sock_path, hello_fields=fields)
    await srv.start()
    async with running(make_client(sock_path)) as c:
        await c.wait_connected(2)
        assert c.site_id == "local" and c.regions == ["local"]
        assert c.egress_class is None and c.capacity is None
    await srv.close()
