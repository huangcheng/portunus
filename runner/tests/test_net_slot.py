"""Network-listener slot management (tls/tcp carriage): eviction + hello deadline.

Security-relevant behavior per the fleet design: on a network listener the sole-client
slot must never be held by an *unauthenticated* peer — not a silent connection, and not
one that sent junk frames (frames_read must not grant immunity).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from portunus_shared import encode_frame, read_frame
from portunus_shared.wire import WIRE_VERSION
from portunus_runner import session as session_mod
from portunus_runner.main import RunnerNetServer

pytestmark = pytest.mark.anyio

TOKEN = "tok-net"


def _hello_frame(token: str) -> bytes:
    return encode_frame(
        {
            "version": WIRE_VERSION,
            "op": "hello",
            "request_id": uuid.uuid4().hex,
            "token": token,
            "gateway_epoch": "e1",
            "conn_id": "c1",
        }
    )


async def _start_server(tmp_path, monkeypatch, *, deadline: float = 5.0):
    monkeypatch.setattr(session_mod, "HELLO_DEADLINE_S", deadline)
    srv = RunnerNetServer(
        host="127.0.0.1",
        port=0,
        ssl_ctx=None,
        token=TOKEN,
        heartbeat_s=5.0,
        grace_s=10.0,
        dev=True,
        policy_path=str(tmp_path / "policy.json"),
    )
    (tmp_path / "policy.json").write_text(json.dumps({"trusted_path": [], "binary_allowlist": []}))
    await srv.start()
    return srv


async def test_silent_preauth_connection_is_evicted_by_newcomer(tmp_path, monkeypatch):
    srv = await _start_server(tmp_path, monkeypatch)
    try:
        silent_reader, silent_writer = await asyncio.open_connection("127.0.0.1", srv.port)
        await asyncio.sleep(0.1)  # let the runner accept and claim the slot
        good_reader, good_writer = await asyncio.open_connection("127.0.0.1", srv.port)
        good_writer.write(_hello_frame(TOKEN))
        await good_writer.drain()
        reply = await asyncio.wait_for(read_frame(good_reader), 5)
        assert reply["ok"] is True  # newcomer evicted the silent incumbent and authenticated
        # The evicted silent connection observes EOF.
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await asyncio.wait_for(read_frame(silent_reader), 5)
        silent_writer.close()
        good_writer.close()
    finally:
        await srv.close()


async def test_junk_frame_does_not_grant_slot_immunity(tmp_path, monkeypatch):
    """A frame with a bad token must not make the incumbent un-evictable."""
    srv = await _start_server(tmp_path, monkeypatch)
    try:
        bad_reader, bad_writer = await asyncio.open_connection("127.0.0.1", srv.port)
        bad_writer.write(_hello_frame("wrong-token"))
        await bad_writer.drain()
        await asyncio.sleep(0.1)  # frame read (frames_read>0) but never authenticated
        good_reader, good_writer = await asyncio.open_connection("127.0.0.1", srv.port)
        good_writer.write(_hello_frame(TOKEN))
        await good_writer.drain()
        reply = await asyncio.wait_for(read_frame(good_reader), 5)
        assert reply["ok"] is True
        # The bad client first gets its `unauthorized` reply, then EOF (evicted/ended).
        err = await asyncio.wait_for(read_frame(bad_reader), 5)
        assert err["ok"] is not True
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await asyncio.wait_for(read_frame(bad_reader), 5)
        bad_writer.close()
        good_writer.close()
    finally:
        await srv.close()


async def test_unauthenticated_connection_dropped_after_hello_deadline(tmp_path, monkeypatch):
    srv = await _start_server(tmp_path, monkeypatch, deadline=0.2)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await asyncio.wait_for(read_frame(reader), 3)
        writer.close()
    finally:
        await srv.close()
