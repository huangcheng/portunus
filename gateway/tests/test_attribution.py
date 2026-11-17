"""Call attribution: timing on CallToolResult ``_meta.portunus``, gate trace_id, fingerprint."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from portunus_gateway import __version__ as gateway_version
from portunus_gateway.omni import Omni, _error_response, with_portunus_meta, error_result
from portunus_gateway.slots import ReserveResult
from portunus_gateway.telemetry import setup_telemetry
from portunus_shared import package_fingerprint

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _otel():
    setup_telemetry(service_version=gateway_version)


def _portunus(result) -> dict:
    meta = result.meta or {}
    portunus = meta.get("io.portunus.gateway") or meta.get("portunus")
    assert isinstance(portunus, dict), meta
    # Dual-write during the rename window.
    assert meta.get("portunus") == portunus
    assert meta.get("io.portunus.gateway") == portunus
    return portunus


async def test_fake_delayed_upstream_reports_upstream_ms(caplog):
    """Runner timing with a ~2s upstream delay surfaces as upstream_ms ≥ 1900."""
    slots = MagicMock()
    slots.reserve_and_start = AsyncMock(
        return_value=ReserveResult(child_id="child1", cold_start=False)
    )
    slots.site_of = MagicMock(return_value="local")
    slots.slot = MagicMock(
        return_value=SimpleNamespace(pending_stop=False, preferred_miss=False, site_id="local")
    )
    slots.stop_pair = MagicMock()

    pool = MagicMock()
    pool.request = AsyncMock(
        return_value={
            "ok": True,
            "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
            "timing": {"queue_ms": 1, "upstream_ms": 2005},
        }
    )

    registry = MagicMock()
    registry.start_payload = AsyncMock(
        return_value={"argv": ["x"], "env": {}, "vault_version": 1, "preferred_regions": []}
    )
    registry.record_outcome = AsyncMock()
    registry.remote = None

    authz = MagicMock()
    authz.is_revoked = MagicMock(return_value=False)
    authz.has_grant = MagicMock(return_value=True)

    omni = Omni(
        session_factory=MagicMock(),
        registry=registry,
        slots=slots,
        runner=pool,
        authz=authz,
    )
    server = SimpleNamespace(id=__import__("uuid").uuid4(), slug="demo", enabled=True, kind="stdio")
    tool = SimpleNamespace(upstream_name="slow", schema={"type": "object"}, schema_hash="h")

    async def granted(key_id, slug):
        return server

    async def enabled_tool(srv, name):
        return tool

    omni._granted_server = granted  # noqa: SLF001
    omni._enabled_tool = enabled_tool  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="portunus.gateway.calls"):
        result = await omni.dispatch(
            "key1",
            "portunus_invoke_tool",
            {"server": "demo", "tool": "slow", "arguments": {}},
        )

    assert not result.is_error
    meta = _portunus(result)
    assert meta["upstream_ms"] >= 1900
    assert meta["trace_id"]
    assert "gateway_ms" in meta
    assert meta.get("queue_ms") == 1
    assert "init_ms" not in meta  # runner did not report init
    assert meta.get("schema_unpinned") is True  # no schema_hash → soft-warn

    records = [r for r in caplog.records if r.name == "portunus.gateway.calls"]
    assert records
    assert meta["trace_id"] in records[-1].getMessage()


async def test_missing_runner_timing_sets_upstream_omits_queue_init():
    slots = MagicMock()
    slots.reserve_and_start = AsyncMock(
        return_value=ReserveResult(child_id="child1", cold_start=True)
    )
    slots.site_of = MagicMock(return_value="local")
    slots.slot = MagicMock(
        return_value=SimpleNamespace(pending_stop=False, preferred_miss=False, site_id="local")
    )

    async def slow_rpc(*_a, **_k):
        import asyncio

        await asyncio.sleep(0.05)
        return {
            "ok": True,
            "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
        }

    pool = MagicMock()
    pool.request = AsyncMock(side_effect=slow_rpc)

    registry = MagicMock()
    registry.start_payload = AsyncMock(
        return_value={"argv": ["x"], "env": {}, "vault_version": 1, "preferred_regions": []}
    )
    registry.record_outcome = AsyncMock()
    authz = MagicMock()
    authz.is_revoked = MagicMock(return_value=False)
    authz.has_grant = MagicMock(return_value=True)

    omni = Omni(
        session_factory=MagicMock(),
        registry=registry,
        slots=slots,
        runner=pool,
        authz=authz,
    )
    server = SimpleNamespace(id=__import__("uuid").uuid4(), slug="demo", enabled=True, kind="stdio")
    tool = SimpleNamespace(upstream_name="t", schema={"type": "object"}, schema_hash="h")
    omni._granted_server = AsyncMock(return_value=server)  # noqa: SLF001
    omni._enabled_tool = AsyncMock(return_value=tool)  # noqa: SLF001

    result = await omni.dispatch(
        "key1", "portunus_invoke_tool", {"server": "demo", "tool": "t", "arguments": {}}
    )
    meta = _portunus(result)
    assert "upstream_ms" in meta
    assert meta["upstream_ms"] >= 40
    assert "queue_ms" not in meta
    assert "init_ms" not in meta
    assert meta["cold_start"] is True
    assert "start_ms" in meta
    assert meta.get("schema_unpinned") is True


async def test_schema_hash_pin_suppresses_schema_unpinned():
    """Pinned invoke must not soft-warn; stale pin still schema_drift."""
    slots = MagicMock()
    slots.reserve_and_start = AsyncMock(
        return_value=ReserveResult(child_id="child1", cold_start=False)
    )
    slots.site_of = MagicMock(return_value="local")
    slots.slot = MagicMock(
        return_value=SimpleNamespace(pending_stop=False, preferred_miss=False, site_id="local")
    )

    pool = MagicMock()
    pool.request = AsyncMock(
        return_value={
            "ok": True,
            "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
        }
    )
    registry = MagicMock()
    registry.start_payload = AsyncMock(
        return_value={"argv": ["x"], "env": {}, "vault_version": 1, "preferred_regions": []}
    )
    registry.record_outcome = AsyncMock()
    authz = MagicMock()
    authz.is_revoked = MagicMock(return_value=False)
    authz.has_grant = MagicMock(return_value=True)

    omni = Omni(
        session_factory=MagicMock(),
        registry=registry,
        slots=slots,
        runner=pool,
        authz=authz,
    )
    server = SimpleNamespace(id=__import__("uuid").uuid4(), slug="demo", enabled=True, kind="stdio")
    tool = SimpleNamespace(upstream_name="t", schema={"type": "object"}, schema_hash="abc")
    omni._granted_server = AsyncMock(return_value=server)  # noqa: SLF001
    omni._enabled_tool = AsyncMock(return_value=tool)  # noqa: SLF001

    pinned = await omni.dispatch(
        "key1",
        "portunus_invoke_tool",
        {"server": "demo", "tool": "t", "arguments": {}, "schema_hash": "abc"},
    )
    assert not pinned.is_error
    assert "schema_unpinned" not in _portunus(pinned)

    stale = await omni.dispatch(
        "key1",
        "portunus_invoke_tool",
        {"server": "demo", "tool": "t", "arguments": {}, "schema_hash": "0" * 64},
    )
    assert stale.is_error
    err = (stale.structured_content or {}).get("error") or {}
    assert err.get("code") == "schema_drift"


async def test_meta_portunus_trace_id_on_success_and_error():
    authz = MagicMock()
    authz.is_revoked = MagicMock(return_value=False)
    omni = Omni(
        session_factory=MagicMock(),
        registry=MagicMock(),
        slots=MagicMock(),
        runner=MagicMock(),
        authz=authz,
    )
    # Discovery success path with empty DB session is awkward; exercise dispatch error + ok_result meta.
    err = await omni.dispatch("key1", "portunus_nope", {})
    assert err.is_error
    assert _portunus(err)["trace_id"]
    assert "gateway_ms" in _portunus(err)

    ok = with_portunus_meta(error_result("unsupported", "x"), {"trace_id": "abc", "gateway_ms": 1})
    assert ok.meta["io.portunus.gateway"]["trace_id"] == "abc"
    assert ok.meta["portunus"]["trace_id"] == "abc"

    # Successful discover-shaped path: revoked key still gets meta via dispatch.
    authz.is_revoked = MagicMock(return_value=True)
    revoked = await omni.dispatch("key1", "portunus_discover_servers", {})
    assert revoked.is_error
    meta = _portunus(revoked)
    assert len(meta["trace_id"]) == 32
    assert isinstance(meta["gateway_ms"], int)


def test_fingerprint_changes_version_when_source_changes(tmp_path):
    pkg = tmp_path / "portunus_gateway"
    pkg.mkdir()
    (pkg / "a.py").write_text("v = 1\n", encoding="utf-8")
    before = package_fingerprint(pkg)
    (pkg / "a.py").write_text("v = 2\n", encoding="utf-8")
    after = package_fingerprint(pkg)
    assert before != after
    # Gateway package version embeds a fingerprint of this shape.
    assert "+" in gateway_version
    base, fp = gateway_version.rsplit("+", 1)
    assert base
    assert len(fp) == 12


async def test_gate_401_includes_error_data_trace_id():
    response = _error_response(401, "unauthorized", "agent key required")
    # Starlette JSONResponse: render body
    body = response.body
    import json

    data = json.loads(body)
    tid = data["error"]["data"]["trace_id"]
    assert isinstance(tid, str) and len(tid) == 32
    assert data["error"]["data"]["code"] == "unauthorized"


async def test_dispatch_attaches_meta_via_asgi_scope_headers():
    """HTTP traceparent is accepted when params meta is absent (unit on extract+dispatch)."""
    parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    authz = MagicMock()
    authz.is_revoked = MagicMock(return_value=True)
    omni = Omni(
        session_factory=MagicMock(),
        registry=MagicMock(),
        slots=MagicMock(),
        runner=MagicMock(),
        authz=authz,
    )
    result = await omni.dispatch(
        "key1",
        "portunus_discover_servers",
        {},
        http_headers={"traceparent": parent},
    )
    assert _portunus(result)["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_extract_parent_context_none_keeps_active_span():
    """No inbound traceparent must not wipe an active parent (e.g. POST /mcp)."""
    from portunus_gateway import telemetry as tel

    assert tel.extract_parent_context(None, None) is None
    assert tel.extract_parent_context({}, {}) is None
    with tel.start_span("portunus.test.parent") as parent:
        want = format(parent.get_span_context().trace_id, "032x")
        with tel.use_parent_context(tel.extract_parent_context(None, None)):
            with tel.start_span("portunus.test.child") as child:
                assert format(child.get_span_context().trace_id, "032x") == want
                assert child.get_span_context().trace_id == parent.get_span_context().trace_id


async def test_dispatch_inherits_active_span_without_traceparent():
    from portunus_gateway import telemetry as tel

    authz = MagicMock()
    authz.is_revoked = MagicMock(return_value=True)
    omni = Omni(
        session_factory=MagicMock(),
        registry=MagicMock(),
        slots=MagicMock(),
        runner=MagicMock(),
        authz=authz,
    )
    with tel.start_span("POST /mcp") as gate:
        want = format(gate.get_span_context().trace_id, "032x")
        revoked = await omni.dispatch("key1", "portunus_discover_servers", {})
    assert _portunus(revoked)["trace_id"] == want
