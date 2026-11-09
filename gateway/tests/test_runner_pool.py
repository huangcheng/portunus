"""RunnerPool + per-slot site routing (fleet design): N runner links, one owning site per child."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from portunus_shared import PortunusError

from portunus_gateway.runner_client import RunnerRequestError
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.scheduler import RunnerInfo
from portunus_gateway.slots import SlotManager, SlotState

pytestmark = pytest.mark.anyio

PAYLOAD = {
    "argv": ["npx", "-y", "--ignore-scripts", "demo@1.2.3"],
    "trailing_allowlist": [],
    "env": {},
    "vault_version": 1,
}


class FakeSite:
    """In-memory RunnerClient stand-in that advertises a site and records what it was sent."""

    def __init__(self, site_id: str, regions: tuple[str, ...] = ("local",)) -> None:
        self.site_id = site_id
        self.regions = list(regions)
        self.connected = True
        self.conn_seq = 1
        self.children: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.on_connected = None
        self.on_child_exited = None

    @property
    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def kills(self) -> list[str]:
        return [f["child_id"] for op, f in self.calls if op == "kill"]

    async def request(self, op, *, timeout, conn_seq=None, **fields):
        if not self.connected or (conn_seq is not None and conn_seq != self.conn_seq):
            raise RunnerRequestError("runner_unavailable", "not connected", sent=False)
        self.calls.append((op, fields))
        if op == "start":
            cid = fields["child_id"]
            self.children[cid] = {"child_id": cid, "server_id": fields["server_id"]}
            return {"child_id": cid, "pid": 1}
        if op == "kill":
            cid = fields["child_id"]
            if cid not in self.children:
                raise RunnerRequestError(
                    "unsupported", "unknown child", sent=True, reason="unknown_child"
                )
            del self.children[cid]
            asyncio.get_running_loop().call_soon(self.exit_cb, cid)
            return {"child_id": cid}
        if op == "inventory":
            return {"children": list(self.children.values())}
        if op == "rpc":
            return {"result": {"content": [{"type": "text", "text": self.site_id}]}}
        raise AssertionError(op)

    def exit_cb(self, cid: str) -> None:
        if self.on_child_exited is not None:
            self.on_child_exited(cid)


def grant(authz, key_id: str, server_id: uuid.UUID) -> None:
    authz._grants.add((key_id, server_id))
    authz.bump()


async def settle(cond, *, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


@pytest.fixture
def site_a():
    return FakeSite("site-a", ("region-a",))


@pytest.fixture
def site_b():
    return FakeSite("site-b", ("region-b",))


@pytest.fixture
def pool(site_a, site_b):
    return RunnerPool([site_a, site_b])


@pytest.fixture
def manager(pool, authz):
    sm = SlotManager(pool, authz, start_ack_s=0.3, stop_retry_s=0.05)
    sm.attach(pool)
    return sm


# ---- pool membership / pick / request -------------------------------------------------


async def test_connected_lists_only_live_clients_with_metadata(pool, site_a, site_b):
    infos = {i.site_id: i for i in pool.connected()}
    assert infos == {
        "site-a": RunnerInfo("site-a", ("region-a",), 0),
        "site-b": RunnerInfo("site-b", ("region-b",), 0),
    }
    site_b.connected = False
    assert [i.site_id for i in pool.connected()] == ["site-a"]


async def test_connected_reports_supplied_load(pool):
    infos = {i.site_id: i.load for i in pool.connected(loads={"site-a": 3})}
    assert infos == {"site-a": 3, "site-b": 0}


async def test_pick_prefers_region_then_falls_back(pool, site_a, site_b):
    d = pool.pick(["region-b"])
    assert (d.site_id, d.preferred_miss) == ("site-b", False)
    site_b.connected = False
    d = pool.pick(["region-b"])
    assert (d.site_id, d.preferred_miss) == ("site-a", True)


async def test_pick_least_loaded_without_preference(pool):
    assert pool.pick([], loads={"site-a": 2, "site-b": 1}).site_id == "site-b"


async def test_empty_pool_pick_is_none():
    assert RunnerPool().pick(["region-a"]) is None
    assert RunnerPool().connected() == []


async def test_request_routes_to_named_site(pool, site_a, site_b):
    reply = await pool.request("site-b", "rpc", timeout=1, child_id="c1")
    assert reply == {"result": {"content": [{"type": "text", "text": "site-b"}]}}
    assert site_a.calls == []
    assert [op for op, _ in site_b.calls] == ["rpc"]


async def test_request_unknown_or_down_site_is_runner_unavailable(pool, site_a):
    with pytest.raises(RunnerRequestError) as ei:
        await pool.request("nope", "rpc", timeout=1)
    assert ei.value.code == "runner_unavailable" and ei.value.sent is False
    site_a.connected = False
    with pytest.raises(RunnerRequestError) as ei:
        await pool.request("site-a", "rpc", timeout=1)
    assert ei.value.code == "runner_unavailable" and ei.value.sent is False


async def test_add_remove_and_duplicate_site(site_a, site_b):
    pool = RunnerPool()
    pool.add(site_a, site_id="site-a")
    pool.add(site_b, site_id="site-b")
    with pytest.raises(ValueError):
        pool.add(FakeSite("x"), site_id="site-a")
    assert pool.remove("site-a") is site_a
    assert pool.remove("site-a") is None
    assert [i.site_id for i in pool.connected()] == ["site-b"]


async def test_single_client_bootstrap_is_a_pool_of_one():
    only = FakeSite("local", ("local",))
    pool = RunnerPool.ensure(only)
    assert RunnerPool.ensure(pool) is pool
    assert pool.pick([]).site_id == "local"
    assert await pool.request("local", "rpc", timeout=1) == {"result": {"content": [{"type": "text", "text": "local"}]}}


async def test_client_without_fleet_attrs_defaults_to_local():
    class Bare:
        connected = True
        conn_seq = 1

        async def request(self, op, *, timeout, conn_seq=None, **fields):
            return {"op": op}

    pool = RunnerPool.ensure(Bare())
    d = pool.pick(["region-a"])
    assert (d.site_id, d.preferred_miss) == ("local", True)


async def test_site_follows_hello_for_unpinned_client():
    c = FakeSite("local", ("local",))
    pool = RunnerPool([c])
    c.site_id, c.regions = "site-a", ["region-a"]  # runner advertised fleet metadata on hello
    assert [i.site_id for i in pool.connected()] == ["site-a"]
    assert (await pool.request("site-a", "rpc", timeout=1))["result"]["content"][0]["text"] == "site-a"


# ---- slots pin and route by site ------------------------------------------------------


async def test_start_pins_slot_to_picked_site(manager, site_a, site_b, authz):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-b"])
    slot = manager.slot(cid)
    assert slot.site_id == "site-b" and slot.state is SlotState.COMMITTED
    assert manager.site_of(cid) == "site-b"
    assert site_a.calls == []
    assert site_b.ops == ["start"]


async def test_start_falls_back_when_preferred_site_down(manager, site_a, site_b, authz):
    site_b.connected = False
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-b"])
    assert manager.slot(cid).site_id == "site-a"
    assert manager.slot(cid).preferred_miss is True


async def test_start_balances_by_slot_load(manager, site_a, site_b, authz):
    sids = [uuid.uuid4() for _ in range(4)]
    for s in sids:
        grant(authz, "k1", s)
    for s in sids:
        await manager.reserve_and_start(s, "k1", PAYLOAD)
    counts = {a: sum(1 for x in manager.slots.values() if x.site_id == a) for a in ("site-a", "site-b")}
    assert counts == {"site-a": 2, "site-b": 2}


async def test_empty_pool_is_runner_unavailable(authz):
    sm = SlotManager(RunnerPool(), authz)
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    with pytest.raises(PortunusError) as ei:
        await sm.reserve_and_start(sid, "k1", PAYLOAD)
    assert ei.value.code == "runner_unavailable"
    assert sm.slots == {}


async def test_all_sites_down_is_runner_unavailable(manager, site_a, site_b, authz):
    site_a.connected = site_b.connected = False
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    with pytest.raises(PortunusError) as ei:
        await manager.reserve_and_start(sid, "k1", PAYLOAD)
    assert ei.value.code == "runner_unavailable"
    assert manager.slots == {}


async def test_stop_hits_owning_site_only(manager, site_a, site_b, authz):
    sa, sb = uuid.uuid4(), uuid.uuid4()
    grant(authz, "k1", sa)
    grant(authz, "k1", sb)
    ca = await manager.reserve_and_start(sa, "k1", PAYLOAD, preferred_regions=["region-a"])
    cb = await manager.reserve_and_start(sb, "k1", PAYLOAD, preferred_regions=["region-b"])
    manager.ungrant("k1", sb)
    await settle(lambda: manager.slot(cb) is None)
    assert site_b.kills() == [cb]
    assert site_a.kills() == []
    assert manager.slot(ca) is not None  # untouched

    manager.revoke_key("k1")
    await settle(lambda: manager.slot(ca) is None)
    assert site_a.kills() == [ca]
    assert site_b.kills() == [cb]


async def test_stop_waits_when_owning_site_is_down_even_if_other_is_up(
    manager, site_a, site_b, authz
):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-a"])
    site_a.connected = False
    manager.ungrant("k1", sid)
    await asyncio.sleep(0.2)
    assert site_b.kills() == []  # never retried against the wrong site
    assert manager.slot(cid) is not None and manager.slot(cid).pending_stop
    site_a.connected = True
    site_a.conn_seq += 1
    res = await manager.sync_with_runner("site-a")
    assert cid in res.stopped
    await settle(lambda: manager.slot(cid) is None)
    assert site_a.kills() == [cid]


# ---- reconcile is scoped to the reporting site ----------------------------------------


async def test_child_on_a_not_adopted_from_b_inventory(manager, site_a, site_b, authz):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-a"])
    # Site B (buggy or hostile) claims the same child id.
    site_b.children[cid] = {"child_id": cid, "server_id": str(sid)}
    res = await manager.sync_with_runner("site-b")
    slot = manager.slot(cid)
    assert slot is not None and slot.site_id == "site-a"
    assert slot.state is SlotState.COMMITTED and not slot.pending_stop
    assert cid not in res.kept
    await settle(lambda: site_b.kills() == [cid])  # foreign copy killed on the reporting site
    assert site_a.kills() == []
    await asyncio.sleep(0.05)  # B's exit frame for it must not free A's slot
    assert manager.slot(cid) is not None and manager.slot(cid).site_id == "site-a"


async def test_b_inventory_does_not_free_a_slots(manager, site_a, site_b, authz):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-a"])
    site_b.conn_seq += 1  # B reconnects with an empty inventory
    res = await manager.sync_with_runner("site-b")
    assert res.freed == []
    assert manager.slot(cid) is not None


async def test_own_site_inventory_absence_frees_slot(manager, site_a, authz):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-a"])
    site_a.children.clear()
    site_a.conn_seq += 1
    res = await manager.sync_with_runner("site-a")
    assert res.freed == [cid] and manager.slot(cid) is None


async def test_unknown_child_from_b_is_tracked_and_killed_on_b(manager, site_a, site_b):
    ghost = uuid.uuid4().hex
    site_b.children[ghost] = {"child_id": ghost, "server_id": str(uuid.uuid4())}
    res = await manager.sync_with_runner("site-b")
    assert res.stopped == [ghost]
    assert site_b.kills() == [ghost] and site_a.kills() == []
    assert manager.slot(ghost) is None  # exit evidence freed it


async def test_child_exited_from_wrong_site_is_ignored(manager, site_a, site_b, authz):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-a"])
    site_b.on_child_exited(cid)  # B cannot give exit evidence for A's child
    assert manager.slot(cid) is not None
    site_a.on_child_exited(cid)
    assert manager.slot(cid) is None


async def test_attach_hooks_reconcile_per_site(manager, site_a, site_b, authz):
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await manager.reserve_and_start(sid, "k1", PAYLOAD, preferred_regions=["region-a"])
    site_b.children[cid] = {"child_id": cid, "server_id": str(sid)}
    await site_b.on_connected()  # what RunnerClient does after B's hello
    assert manager.slot(cid).site_id == "site-a"
    assert site_b.ops.count("inventory") == 1 and "inventory" not in site_a.ops


async def test_multi_site_requires_explicit_site_for_sync(manager):
    with pytest.raises(ValueError):
        await manager.sync_with_runner()


# ---- single-runner compat -------------------------------------------------------------


async def test_bare_client_still_works_without_site_argument(authz):
    only = FakeSite("local", ("local",))
    sm = SlotManager(only, authz, start_ack_s=0.3, stop_retry_s=0.05)
    sm.attach(only)
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    cid = await sm.reserve_and_start(sid, "k1", PAYLOAD)
    assert sm.slot(cid).site_id == "local"
    only.children.clear()
    only.conn_seq += 1
    res = await sm.sync_with_runner()
    assert res.freed == [cid]


# ---- invoke routes through the owning site -------------------------------------


async def test_omni_rpc_goes_to_the_slots_site(pool, manager, site_a, site_b, authz):
    from types import SimpleNamespace

    from portunus_gateway.omni import Omni

    class Registry:
        async def start_payload(self, server_id):
            return PAYLOAD

    omni = Omni(
        session_factory=lambda: None, registry=Registry(), slots=manager, runner=pool, authz=authz
    )
    sid = uuid.uuid4()
    grant(authz, "k1", sid)
    server = SimpleNamespace(id=sid, enabled=True)
    tool = SimpleNamespace(upstream_name="alpha")
    other = uuid.uuid4()  # load site A so the scheduler sends the invoke child to B
    grant(authz, "k1", other)
    await manager.reserve_and_start(other, "k1", PAYLOAD, preferred_regions=["region-a"])
    result = await omni._run_invoke("k1", server, tool, {}, 1.0)  # noqa: SLF001
    owner = next(sl for sl in manager.slots.values() if sl.server_id == sid)
    assert owner.site_id == "site-b"
    assert site_b.ops == ["start", "rpc"]
    assert site_a.ops == ["start"]  # A only ever saw its own child's start
    assert not result.is_error and result.content[0].text == "site-b"
