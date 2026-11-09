"""Slot FSM tests (spec §6.1): reserve -> start -> commit, caps, revoke races, reconcile."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from portunus_shared import DEFAULTS, PortunusError

from portunus_gateway.runner_client import RunnerRequestError
from portunus_gateway.slots import SlotManager, SlotState

pytestmark = pytest.mark.anyio

PAYLOAD = {
    "argv": ["npx", "-y", "--ignore-scripts", "demo@1.2.3"],
    "trailing_allowlist": [],
    "env": {"FOO": "bar"},
    "vault_version": 1,
}


# ---- fakes / helpers ------------------------------------------------------------------


class FakeRunner:
    """In-memory stand-in for RunnerClient (same ``request`` / ``connected`` / ``conn_seq``)."""

    def __init__(self) -> None:
        self.connected = True
        self.conn_seq = 1
        self.children: dict[str, dict] = {}  # live at the "runner"
        self.starts: list[dict] = []
        self.kills: list[str] = []
        self.start_entered = asyncio.Event()
        self.start_gate: asyncio.Event | None = None  # child spawned, ack withheld until set
        self.start_error: RunnerRequestError | None = None
        self.start_hang = False  # never ack: raise upstream_timeout after ``timeout``
        self.start_drop = False  # connection dies after the child spawned
        self.kill_exits = True  # kill really kills + child_exited is delivered
        self.manager: SlotManager | None = None

    def reconnect(self) -> None:
        self.connected = True
        self.conn_seq += 1

    def disconnect(self) -> None:
        self.connected = False

    async def request(self, op, *, timeout, conn_seq=None, **fields):
        if not self.connected or (conn_seq is not None and conn_seq != self.conn_seq):
            raise RunnerRequestError("runner_unavailable", "not connected", sent=False)
        if op == "start":
            self.starts.append(fields)
            if self.start_error is not None:
                raise self.start_error
            cid = fields["child_id"]
            self.children[cid] = {"child_id": cid, "server_id": fields["server_id"], "pid": 1000}
            self.start_entered.set()
            if self.start_hang:
                await asyncio.sleep(timeout)
                raise RunnerRequestError("upstream_timeout", "no ack", sent=True)
            if self.start_drop:
                self.connected = False
                raise RunnerRequestError("runner_unavailable", "lost", sent=True)
            if self.start_gate is not None:
                await self.start_gate.wait()
            return {"child_id": cid, "pid": 1000}
        if op == "kill":
            cid = fields["child_id"]
            self.kills.append(cid)
            if cid not in self.children:
                raise RunnerRequestError(
                    "unsupported", "unknown child", sent=True, reason="unknown_child"
                )
            if self.kill_exits:
                self.exit_child(cid)
            return {"child_id": cid}
        if op == "inventory":
            return {"run_id": "r1", "children": list(self.children.values())}
        raise AssertionError(op)

    def exit_child(self, cid: str) -> None:
        self.children.pop(cid, None)
        assert self.manager is not None
        asyncio.get_running_loop().call_soon(self.manager.on_child_exited, cid)


def grant(authz, key_id: str, server_id: uuid.UUID) -> None:
    authz._grants.add((key_id, server_id))
    authz.bump()


def ungrant(authz, key_id: str, server_id: uuid.UUID) -> None:
    authz._grants.discard((key_id, server_id))
    authz.bump()


async def settle(cond, *, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


@pytest.fixture
def fake_runner():
    return FakeRunner()


@pytest.fixture
def slot_manager(fake_runner, authz):
    sm = SlotManager(fake_runner, authz, start_ack_s=0.3, stop_retry_s=0.05)
    fake_runner.manager = sm
    return sm


def reconcile_kwargs(authz):
    return {
        "is_revoked": authz.is_revoked,
        "has_grant": authz.has_grant,
        "generation": lambda: authz.generation,
    }


# ---- caps -----------------------------------------------------------------------------


async def test_default_caps_match_spec(slot_manager):
    assert slot_manager.max_children_per_key == DEFAULTS.max_children_per_key == 32
    assert slot_manager.max_children_global == DEFAULTS.max_children_global == 256


async def test_cap_per_key(slot_manager, fake_runner, authz):
    servers = [uuid.uuid4() for _ in range(33)]
    for s in servers:
        grant(authz, "k1", s)
    for s in servers[:32]:
        await slot_manager.reserve_and_start(s, "k1", PAYLOAD)
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(servers[32], "k1", PAYLOAD)
    assert ei.value.code == "child_cap"
    assert len(fake_runner.starts) == 32  # the 33rd never reached the runner
    # another key is unaffected by k1's cap
    grant(authz, "k2", servers[0])
    await slot_manager.reserve_and_start(servers[0], "k2", PAYLOAD)


async def test_cap_global(fake_runner, authz):
    sm = SlotManager(fake_runner, authz, max_children_per_key=2, max_children_global=3)
    fake_runner.manager = sm
    srv = uuid.uuid4()
    for k in ("a", "b", "c", "d"):
        grant(authz, k, srv)
    for k in ("a", "b", "c"):
        await sm.reserve_and_start(srv, k, PAYLOAD)
    with pytest.raises(PortunusError) as ei:
        await sm.reserve_and_start(srv, "d", PAYLOAD)
    assert ei.value.code == "child_cap"


async def test_exit_frees_cap(slot_manager, fake_runner, authz):
    servers = [uuid.uuid4() for _ in range(33)]
    for s in servers:
        grant(authz, "k1", s)
    cids = [await slot_manager.reserve_and_start(s, "k1", PAYLOAD) for s in servers[:32]]
    slot_manager.on_child_exited(cids[0])
    await slot_manager.reserve_and_start(servers[32], "k1", PAYLOAD)


async def test_one_child_per_server_key_pair(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    a, b = await asyncio.gather(
        slot_manager.reserve_and_start(srv, "k1", PAYLOAD),
        slot_manager.reserve_and_start(srv, "k1", PAYLOAD),
    )
    assert a == b
    assert len(fake_runner.starts) == 1
    assert await slot_manager.reserve_and_start(srv, "k1", PAYLOAD) == a


# ---- start frame / admission ----------------------------------------------------------


async def test_start_frame_is_explicit(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    f = fake_runner.starts[0]
    assert f["child_id"] == cid and f["server_id"] == str(srv)
    assert f["argv"] == PAYLOAD["argv"]  # command rides as argv[0]
    assert f["env"] == {"FOO": "bar"}
    assert f["trailing_allowlist"] == []
    slot = slot_manager.slot(cid)
    assert slot.state is SlotState.COMMITTED and slot.vault_version == 1


async def test_bad_payload_rejected_without_reserving(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    for bad in ({}, {"argv": []}, {"argv": [1]}, {"argv": ["npx"], "env": {"A": 1}}):
        with pytest.raises(PortunusError) as ei:
            await slot_manager.reserve_and_start(srv, "k1", bad)
        assert ei.value.code == "spawn_rejected"
    assert slot_manager.slots == {} and fake_runner.starts == []


async def test_revoked_checked_before_grant(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    authz.mark_revoked("k1")  # has_grant still answers True for a revoked key
    assert authz.has_grant("k1", srv)
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "revoked"
    assert fake_runner.starts == []


async def test_not_granted(slot_manager, fake_runner, authz):
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(uuid.uuid4(), "k1", PAYLOAD)
    assert ei.value.code == "not_granted"


async def test_server_disabled_admission(fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    sm = SlotManager(fake_runner, authz, server_enabled=lambda s: False)
    with pytest.raises(PortunusError) as ei:
        await sm.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "server_disabled"


async def test_runner_down_does_not_reserve(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.disconnect()
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "runner_unavailable"
    assert slot_manager.slots == {}


# ---- start failure paths --------------------------------------------------------------


async def test_runner_refusal_rolls_back(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    # a runner error *reply* (reply set) means it answered: nothing was started
    fake_runner.start_error = RunnerRequestError(
        "spawn_rejected", "nope", sent=True, reply={"ok": False}
    )
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "spawn_rejected"
    assert slot_manager.slots == {}  # never started: rollback frees immediately
    fake_runner.start_error = None
    await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)


async def test_ack_timeout_marks_pending_stop_and_kills(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_hang = True
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "upstream_timeout"
    (cid,) = fake_runner.children  # runner did spawn it
    slot = slot_manager.slot(cid)
    # Freed only on exit evidence — which the (real) kill produces.
    await settle(lambda: slot_manager.slots == {})
    assert fake_runner.kills[0] == cid and slot.pending_stop


async def test_ack_timeout_slot_reserved_until_exit_evidence(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_hang = True
    fake_runner.kill_exits = False
    with pytest.raises(PortunusError):
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    (cid,) = fake_runner.children
    await settle(lambda: len(fake_runner.kills) >= 2)  # retries kill, never force-frees
    slot = slot_manager.slot(cid)
    assert slot.pending_stop and slot.state is SlotState.RESERVED
    # a restart for the pair must not double-spawn while the old child is unresolved
    fake_runner.start_hang = False
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "runner_unavailable"
    assert len(fake_runner.starts) == 1
    fake_runner.exit_child(cid)
    await settle(lambda: slot_manager.slots == {})


async def test_child_exit_during_start_is_crash(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    (cid,) = fake_runner.children
    slot_manager.on_child_exited(cid)
    fake_runner.start_gate.set()
    with pytest.raises(PortunusError) as ei:
        await task
    assert ei.value.code == "upstream_crash"
    assert slot_manager.slots == {}


async def test_unknown_child_exit_ignored(slot_manager):
    assert slot_manager.on_child_exited("nope") is False


# ---- revoke while a start is in flight ------------------------------------------------


async def test_revoke_during_start(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    fake_runner.kill_exits = False
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    (cid,) = fake_runner.children
    # revoke commits while the start is un-acked; has_grant is still True (stale grant row)
    authz.mark_revoked("k1")
    assert authz.has_grant("k1", srv)
    fake_runner.start_gate.set()
    with pytest.raises(PortunusError) as ei:
        await task
    assert ei.value.code == "revoked"  # never handed back as live

    slot = slot_manager.slot(cid)
    assert slot.pending_stop and slot.state is not SlotState.COMMITTED
    await settle(lambda: cid in fake_runner.kills)
    # Child still alive at the runner (kill not effective yet): reconcile must NOT adopt it.
    fake_runner.reconnect()
    await slot_manager.sync_with_runner()
    slot = slot_manager.slot(cid)
    assert slot is not None and slot.pending_stop and slot.state is not SlotState.COMMITTED
    assert slot_manager.committed_for_key("k1") == []
    # and a new start for the revoked key is refused outright
    with pytest.raises(PortunusError) as ei2:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei2.value.code == "revoked"
    # exit evidence finally frees the slot
    fake_runner.exit_child(cid)
    await settle(lambda: slot_manager.slots == {})


async def test_revoke_during_start_kills_child_and_frees(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    authz.mark_revoked("k1")
    fake_runner.start_gate.set()
    with pytest.raises(PortunusError) as ei:
        await task
    assert ei.value.code == "revoked"
    await settle(lambda: slot_manager.slots == {} and fake_runner.children == {})


async def test_revoke_key_hook_flags_inflight_start_even_if_authz_unchanged(
    slot_manager, fake_runner, authz
):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    (cid,) = fake_runner.children
    slot_manager.revoke_key("k1")  # explicit hook: pending_stop on all of the key's children
    assert slot_manager.slot(cid).pending_stop
    fake_runner.start_gate.set()
    with pytest.raises(PortunusError) as ei:
        await task
    assert ei.value.code == "cancelled"
    await settle(lambda: slot_manager.slots == {})


async def test_ungrant_during_start(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    ungrant(authz, "k1", srv)
    fake_runner.start_gate.set()
    with pytest.raises(PortunusError) as ei:
        await task
    assert ei.value.code == "not_granted"
    await settle(lambda: slot_manager.slots == {} and fake_runner.children == {})


async def test_unrelated_generation_bump_does_not_cancel_start(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    g0 = authz.generation
    grant(authz, "other", uuid.uuid4())  # bumps generation; our pair untouched
    assert authz.generation > g0
    fake_runner.start_gate.set()
    cid = await task
    slot = slot_manager.slot(cid)
    assert slot.state is SlotState.COMMITTED
    assert slot.generation == authz.generation  # re-stamped at commit


async def test_vault_bump_during_start_cancels(fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    versions = {srv: 1}
    sm = SlotManager(fake_runner, authz, vault_version=lambda s: versions[s], stop_retry_s=0.05)
    fake_runner.manager = sm
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(sm.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    versions[srv] = 2
    fake_runner.start_gate.set()
    with pytest.raises(PortunusError) as ei:
        await task
    assert ei.value.code == "cancelled"
    await settle(lambda: sm.slots == {})


# ---- reconcile ------------------------------------------------------------------------


async def test_reconcile_drops_ungranted(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    ungrant(authz, "k1", srv)  # slot manager not told: reconcile must still catch it
    fake_runner.kill_exits = False
    inv = (await fake_runner.request("inventory", timeout=1))["children"]
    res = await slot_manager.reconcile(inv, **reconcile_kwargs(authz))
    assert cid in res.stopped and cid in fake_runner.kills
    assert slot_manager.slot(cid) is not None  # not freed without exit evidence
    assert slot_manager.committed_for_key("k1") == []
    fake_runner.exit_child(cid)
    await settle(lambda: slot_manager.slots == {})


async def test_same_epoch_reconnect_after_ungrant_during_grace_kills_not_adopts(
    slot_manager, fake_runner, authz
):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    # Gateway loses the runner; runner keeps the child alive in its local grace window.
    fake_runner.disconnect()
    ungrant(authz, "k1", srv)
    slot_manager.ungrant("k1", srv)  # unreachable: stays pending_stop, kill cannot be sent
    await asyncio.sleep(0.15)
    assert fake_runner.kills == []
    assert slot_manager.slot(cid).pending_stop
    # Same-epoch reconnect before grace expiry: runner still lists the child.
    fake_runner.reconnect()
    fake_runner.kill_exits = False
    await slot_manager.sync_with_runner()
    assert cid in fake_runner.kills
    slot = slot_manager.slot(cid)
    assert slot.pending_stop and slot_manager.committed_for_key("k1") == []
    with pytest.raises(PortunusError):
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    fake_runner.exit_child(cid)
    await settle(lambda: slot_manager.slots == {})


async def test_reconnect_after_ungrant_without_gateway_hook_still_kills(
    slot_manager, fake_runner, authz
):
    """Ungrant happened while disconnected and no hook ran (e.g. gateway-side state only)."""
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    fake_runner.disconnect()
    ungrant(authz, "k1", srv)
    fake_runner.reconnect()
    await slot_manager.sync_with_runner()  # exit via kill_exits
    assert cid in fake_runner.kills
    await settle(lambda: slot_manager.slots == {})


async def test_reconcile_revoked_key_killed(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    authz.mark_revoked("k1")
    fake_runner.reconnect()
    await slot_manager.sync_with_runner()
    assert cid in fake_runner.kills
    await settle(lambda: slot_manager.slots == {})


async def test_reconcile_adopts_allowed_and_frees_absent(slot_manager, fake_runner, authz):
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    grant(authz, "k1", s1)
    grant(authz, "k1", s2)
    c1 = await slot_manager.reserve_and_start(s1, "k1", PAYLOAD)
    c2 = await slot_manager.reserve_and_start(s2, "k1", PAYLOAD)
    fake_runner.children.pop(c2)  # runner lost it (e.g. grace expiry, never reported)
    fake_runner.reconnect()
    res = await slot_manager.sync_with_runner()
    assert c1 in res.kept and c2 in res.freed
    assert slot_manager.slot(c1).state is SlotState.COMMITTED
    assert slot_manager.slot(c2) is None
    assert fake_runner.kills == []


async def test_reconcile_adopts_unresolved_reserved_start(slot_manager, fake_runner, authz):
    """Connection dies after spawn but before ack: listed + allowed -> adopt (no double spawn)."""
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_drop = True
    with pytest.raises(PortunusError) as ei:
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    assert ei.value.code == "runner_unavailable"
    (cid,) = fake_runner.children
    assert slot_manager.slot(cid).state is SlotState.RESERVED
    fake_runner.start_drop = False
    fake_runner.reconnect()
    res = await slot_manager.sync_with_runner()
    assert cid in res.kept
    assert slot_manager.slot(cid).state is SlotState.COMMITTED
    assert await slot_manager.reserve_and_start(srv, "k1", PAYLOAD) == cid
    assert len(fake_runner.starts) == 1


async def test_reconcile_frees_unresolved_start_absent_from_inventory(
    slot_manager, fake_runner, authz
):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_drop = True
    with pytest.raises(PortunusError):
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    fake_runner.children.clear()  # never actually ran / reaped
    fake_runner.start_drop = False
    fake_runner.reconnect()
    res = await slot_manager.sync_with_runner()
    assert len(res.freed) == 1 and slot_manager.slots == {}


async def test_reconcile_does_not_free_start_sent_on_current_connection(
    slot_manager, fake_runner, authz
):
    """An in-flight start sent after the inventory request must not be freed by absence."""
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_gate = asyncio.Event()
    task = asyncio.create_task(slot_manager.reserve_and_start(srv, "k1", PAYLOAD))
    await fake_runner.start_entered.wait()
    (cid,) = fake_runner.children
    await slot_manager.reconcile([], **reconcile_kwargs(authz))  # stale inventory: empty
    assert slot_manager.slot(cid) is not None
    fake_runner.start_gate.set()
    assert await task == cid


async def test_reconcile_does_not_adopt_timed_out_pending_stop(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.start_hang = True
    fake_runner.kill_exits = False
    with pytest.raises(PortunusError):
        await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    (cid,) = fake_runner.children
    fake_runner.reconnect()
    res = await slot_manager.sync_with_runner()
    assert cid in res.stopped and cid not in res.kept
    assert slot_manager.slot(cid).state is SlotState.RESERVED
    fake_runner.exit_child(cid)
    await settle(lambda: slot_manager.slots == {})


async def test_reconcile_kills_unknown_inventory_child(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    fake_runner.children["ghost"] = {"child_id": "ghost", "server_id": str(srv), "pid": 9}
    fake_runner.reconnect()
    res = await slot_manager.sync_with_runner()
    assert "ghost" in res.stopped and "ghost" in fake_runner.kills
    await settle(lambda: slot_manager.slots == {})
    # garbage server ids are tolerated too
    fake_runner.children["ghost2"] = {"child_id": "ghost2", "server_id": "not-a-uuid"}
    await slot_manager.sync_with_runner()
    assert "ghost2" in fake_runner.kills


async def test_reconcile_vault_version_mismatch_killed(fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    versions = {srv: 1}
    sm = SlotManager(fake_runner, authz, vault_version=lambda s: versions[s], stop_retry_s=0.05)
    fake_runner.manager = sm
    cid = await sm.reserve_and_start(srv, "k1", PAYLOAD)
    versions[srv] = 2
    fake_runner.reconnect()
    await sm.sync_with_runner()
    assert cid in fake_runner.kills
    await settle(lambda: sm.slots == {})


async def test_reconcile_disabled_server_killed(fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    enabled = {"v": True}
    sm = SlotManager(fake_runner, authz, server_enabled=lambda s: enabled["v"], stop_retry_s=0.05)
    fake_runner.manager = sm
    cid = await sm.reserve_and_start(srv, "k1", PAYLOAD)
    enabled["v"] = False
    fake_runner.reconnect()
    await sm.sync_with_runner()
    assert cid in fake_runner.kills


async def test_disable_server_hook_stops_all_keys(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    for k in ("a", "b"):
        grant(authz, k, srv)
        await slot_manager.reserve_and_start(srv, k, PAYLOAD)
    other = uuid.uuid4()
    grant(authz, "a", other)
    keep = await slot_manager.reserve_and_start(other, "a", PAYLOAD)
    slot_manager.disable_server(srv)
    await settle(lambda: set(slot_manager.slots) == {keep})
    assert len(fake_runner.kills) == 2


async def test_kill_unknown_child_reply_is_exit_evidence(slot_manager, fake_runner, authz):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    fake_runner.children.pop(cid)  # runner no longer has it; child_exited was lost
    slot_manager.revoke_key("k1")
    await settle(lambda: slot_manager.slots == {})


async def test_stop_gives_up_while_disconnected_and_resumes_via_reconcile(
    slot_manager, fake_runner, authz
):
    srv = uuid.uuid4()
    grant(authz, "k1", srv)
    cid = await slot_manager.reserve_and_start(srv, "k1", PAYLOAD)
    fake_runner.disconnect()
    slot_manager.revoke_key("k1")
    await asyncio.sleep(0.1)
    assert slot_manager.slot(cid) is not None and fake_runner.kills == []  # no force-free
    authz.mark_revoked("k1")
    fake_runner.reconnect()
    await slot_manager.sync_with_runner()
    await settle(lambda: slot_manager.slots == {})
