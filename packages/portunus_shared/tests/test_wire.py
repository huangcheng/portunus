import asyncio
import json

import pytest

from portunus_shared.errors import PortunusError
from portunus_shared.wire import (
    MAX_FRAME_BODY,
    OPS,
    skill_exec,
    WIRE_VERSION,
    FrameError,
    cancel,
    child_exited,
    encode_frame,
    heartbeat,
    hello,
    inventory,
    kill,
    probe,
    read_frame,
    rpc,
    start,
    stop,
    validate_message,
)


class R:
    """Feedable reader exposing only readexactly (StreamReader subset)."""

    def __init__(self, data: bytes):
        self._data = data

    async def readexactly(self, n):
        out, self._data = self._data[:n], self._data[n:]
        if len(out) < n:
            raise asyncio.IncompleteReadError(out, n)
        return out


def test_roundtrip():
    raw = encode_frame({"version": 1, "op": "heartbeat", "request_id": "a"})
    assert int.from_bytes(raw[:4], "big") == len(raw) - 4
    obj = asyncio.run(read_frame(R(raw)))
    assert obj["op"] == "heartbeat"
    assert obj["request_id"] == "a"


def test_roundtrip_multiple_frames_and_utf8():
    raw = encode_frame({"version": 1, "op": "rpc", "text": "héllo ☃"}) + encode_frame(
        {"version": 1, "op": "heartbeat"}
    )
    reader = R(raw)

    async def _go():
        return await read_frame(reader), await read_frame(reader)

    a, b = asyncio.run(_go())
    assert a["text"] == "héllo ☃"
    assert b["op"] == "heartbeat"


def test_constants():
    assert MAX_FRAME_BODY == 5 * 1024 * 1024
    assert WIRE_VERSION == 1
    assert OPS == frozenset(
        {
            "hello",
            "inventory",
            "start",
            "stop",
            "kill",
            "probe",
            "rpc",
            "cancel",
            "heartbeat",
            "child_exited",
            "skill_exec",
        }
    )


def test_oversize_rejected():
    with pytest.raises(FrameError):
        encode_frame({"blob": "x" * (5 * 1024 * 1024)})


def test_exactly_max_body_accepted():
    overhead = len(json.dumps({"b": ""}, separators=(",", ":")).encode())
    raw = encode_frame({"b": "x" * (MAX_FRAME_BODY - overhead)})
    assert len(raw) - 4 == MAX_FRAME_BODY


def test_frame_error_is_portunus_error_payload_too_large():
    with pytest.raises(PortunusError) as ei:
        encode_frame({"blob": "x" * MAX_FRAME_BODY})
    assert ei.value.code == "payload_too_large"


def test_read_oversize_header_rejected_before_body_read():
    hdr = (MAX_FRAME_BODY + 1).to_bytes(4, "big")
    with pytest.raises(FrameError) as ei:
        asyncio.run(read_frame(R(hdr)))  # no body supplied: must fail on header
    assert ei.value.code == "payload_too_large"


def test_read_truncated_raises_incomplete():
    raw = encode_frame({"version": 1, "op": "heartbeat"})
    with pytest.raises(asyncio.IncompleteReadError):
        asyncio.run(read_frame(R(raw[:-1])))
    with pytest.raises(asyncio.IncompleteReadError):
        asyncio.run(read_frame(R(b"")))


def test_read_invalid_json_and_non_object():
    bad = b"{nope"
    with pytest.raises(FrameError):
        asyncio.run(read_frame(R(len(bad).to_bytes(4, "big") + bad)))
    arr = b"[1,2]"
    with pytest.raises(FrameError):
        asyncio.run(read_frame(R(len(arr).to_bytes(4, "big") + arr)))
    bad_utf8 = b"\xff\xfe"
    with pytest.raises(FrameError):
        asyncio.run(read_frame(R(len(bad_utf8).to_bytes(4, "big") + bad_utf8)))


def test_encode_rejects_non_dict_and_unserializable():
    with pytest.raises(FrameError):
        encode_frame([1])  # type: ignore[arg-type]
    with pytest.raises(FrameError):
        encode_frame({"x": object()})


def test_works_with_real_stream_reader():
    async def _go():
        r = asyncio.StreamReader()
        r.feed_data(encode_frame(hello(request_id="r1", token="t")))
        r.feed_eof()
        return await read_frame(r)

    assert asyncio.run(_go())["op"] == "hello"


def test_helpers_build_versioned_messages():
    common = dict(request_id="r", token="tok")
    msgs = {
        "hello": hello(**common),
        "inventory": inventory(**common),
        "start": start(
            **common,
            child_id="c1",
            server_id="s1",
            argv=["npx", "--ignore-scripts", "pkg@1.2.3"],
            trailing_allowlist=[],
        ),
        "stop": stop(**common, child_id="c1"),
        "kill": kill(**common, child_id="c1"),
        "probe": probe(**common, child_id="c1"),
        "rpc": rpc(**common, child_id="c1", payload={"method": "tools/list"}),
        "cancel": cancel(**common, target_request_id="r0"),
        "heartbeat": heartbeat(**common),
        "child_exited": child_exited(**common, child_id="c1", exit_code=0),
        "skill_exec": skill_exec(**common, child_id="c1", payload={"argv": ["ls"]}),
    }
    assert set(msgs) == OPS
    for op, m in msgs.items():
        assert m["version"] == 1
        assert m["op"] == op
        assert m["request_id"] == "r"
        assert m["token"] == "tok"
        validate_message(m)
        assert asyncio.run(read_frame(R(encode_frame(m)))) == m


def test_start_defaults_trailing_allowlist_empty():
    m = start(request_id="r", token="t", child_id="c", server_id="s", argv=["x"])
    assert m["trailing_allowlist"] == []


def test_gateway_fields_optional_passthrough():
    m = rpc(
        request_id="r",
        token="t",
        child_id="c",
        payload={},
        gateway_epoch="e",
        conn_id="n",
    )
    assert m["gateway_epoch"] == "e" and m["conn_id"] == "n"
    assert "gateway_epoch" not in heartbeat(request_id="r", token="t")


def test_validate_message_rejects_bad():
    with pytest.raises(FrameError):
        validate_message({"version": 2, "op": "hello"})
    with pytest.raises(FrameError):
        validate_message({"version": 1, "op": "bogus"})
    with pytest.raises(FrameError):
        validate_message({"op": "hello"})
    with pytest.raises(FrameError):
        validate_message({"version": 1})
