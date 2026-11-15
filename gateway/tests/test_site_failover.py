"""Learned per-(server, site) health: auto placement exclusion + child failover.

GFW-shaped scenario: a stdio child on one runner site starts failing with
network-class errors (upstream_timeout); after the threshold, the pair is retired
and the next invoke cold-starts on the other live site — no operator pin needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from portunus_shared import PortunusError

from portunus_gateway.db.models import Base, SiteHealth
from portunus_gateway.registry import (
    NETWORK_FAILURE_CODES,
    SiteHealthPolicy,
    Registry,
)
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.scheduler import RunnerInfo, pick_runner

pytestmark = pytest.mark.anyio


# ---- scheduler tier ----------------------------------------------------------------------


def test_excluded_site_is_skipped_even_when_pinned():
    live = [RunnerInfo(site_id="cn-a", regions=("cn",)), RunnerInfo(site_id="eu", regions=("eu",))]
    # Operator pins cn; the learned mark on cn-a must not pin into the partition.
    d = pick_runner(live, preferred_regions=["cn"], exclude_sites=frozenset({"cn-a"}))
    assert d is not None and d.site_id == "eu" and d.preferred_miss is True


def test_all_sites_marked_falls_back_to_trying():
    live = [RunnerInfo(site_id="cn-a")]
    d = pick_runner(live, preferred_regions=[], exclude_sites=frozenset({"cn-a"}))
    assert d is not None and d.site_id == "cn-a"  # a partition beats a hard refusal


def test_healthy_preferred_site_still_wins():
    live = [RunnerInfo(site_id="cn-a", regions=("cn",)), RunnerInfo(site_id="eu", regions=("eu",))]
    d = pick_runner(live, preferred_regions=["cn"], exclude_sites=frozenset())
    assert d.site_id == "cn-a" and d.preferred_miss is False


# ---- learned health (registry, real DB) --------------------------------------------------


async def _mk_server(db) -> uuid.UUID:
    from portunus_gateway.db.models import Server
    from portunus_gateway.db.session import async_session

    sid = uuid.uuid4()
    async with async_session() as s:
        s.add(Server(id=sid, name="t", slug=f"t-{sid.hex[:8]}", kind="stdio",
                     command="/usr/bin/true", args=[]))
        await s.commit()
    return sid


async def test_site_health_threshold_and_recovery(db, pepper_file):
    import os

    from portunus_gateway.db.session import async_session
    from portunus_gateway.vault import Vault

    vault = Vault({1: os.urandom(32)})
    reg = Registry(async_session, vault, RunnerPool([]), trusted_path=["/usr/bin"], binary_allowlist=[])
    sid = await _mk_server(db)

    async def outcome(code, site):
        await reg.record_outcome(sid, code, site_id=site)

    # One network failure: below threshold, not excluded.
    await outcome("upstream_timeout", "cn-a")
    assert await reg.unhealthy_sites(sid) == set()

    # Second: crosses threshold -> excluded. Link-loss counts too; other sites unaffected.
    await outcome("upstream_timeout", "cn-a")
    assert await reg.unhealthy_sites(sid) == {"cn-a"}
    await outcome("runner_unavailable", "eu")
    assert await reg.unhealthy_sites(sid) == {"cn-a"}  # one failure stays below threshold

    # App/auth errors never touch the site row (no streak growth, no TTL refresh).
    await reg.record_outcome(sid, "auth_required", site_id="eu")
    assert await reg.unhealthy_sites(sid) == {"cn-a"}
    # upstream_crash IS a path signal (GFW / vendor unreachable from that site).
    # eu already has one runner_unavailable above; one crash crosses the threshold.
    await outcome("upstream_crash", "eu")
    assert await reg.unhealthy_sites(sid) == {"cn-a", "eu"}

    # A success clears that site's mark (eu stays marked until it recovers).
    await outcome(None, "cn-a")
    assert await reg.unhealthy_sites(sid) == {"eu"}
    await outcome(None, "eu")
    assert await reg.unhealthy_sites(sid) == set()

    # A caller-chosen tiny timeout is not the site's fault: suppressed entirely.
    await reg.record_outcome(
        sid, "upstream_timeout", site_id="cn-a", caller_shortened_timeout=True
    )
    assert await reg.unhealthy_sites(sid) == set()


async def test_site_health_mark_expires(db, pepper_file):
    import os
    from datetime import datetime, timedelta, timezone

    from portunus_gateway.db.session import async_session
    from portunus_gateway.vault import Vault

    vault = Vault({1: os.urandom(32)})
    reg = Registry(async_session, vault, RunnerPool([]), trusted_path=["/usr/bin"], binary_allowlist=[])
    sid = await _mk_server(db)
    async with async_session() as s:
        s.add(
            SiteHealth(
                server_id=sid,
                site_id="cn-a",
                last_error_code="upstream_timeout",
                last_error_at=datetime.now(timezone.utc) - timedelta(seconds=SiteHealthPolicy.ttl_s + 60),
                consecutive_failures=99,
            )
        )
        await s.commit()
    assert await reg.unhealthy_sites(sid) == set()  # expired: networks heal


# ---- end to end: mark drives placement + pair retirement ---------------------------------


async def test_marked_site_is_not_picked_and_pair_is_retired(authz, db, pepper_file):
    """SlotManager consults the learned marks: a marked site loses the next placement."""
    import os

    from portunus_gateway.db.session import async_session
    from portunus_gateway.registry import Registry as Reg
    from portunus_gateway.slots import SlotManager
    from portunus_gateway.vault import Vault as V
    from portunus_gateway.db.session import async_session as sf

    vault = V({1: os.urandom(32)})
    reg = Reg(sf, vault, RunnerPool([]), trusted_path=["/usr/bin"], binary_allowlist=[])
    sid = await _mk_server(db)

    class _FakePool(RunnerPool):
        def __init__(self):
            super().__init__()
            self.picks: list[frozenset[str]] = []

        def pick(
            self,
            preferred_regions=(),
            *,
            loads=None,
            exclude_sites=frozenset(),
            sticky_site_id=None,
            latency_ms=None,
        ):
            self.picks.append(frozenset(exclude_sites))
            return None

    from portunus_gateway.db.models import AgentKey, Grant
    from portunus_gateway.db.session import async_session as sf2

    async with sf2() as s:
        s.add(AgentKey(key_id="k", digest=b"\x00" * 32))
        s.add(Grant(key_id="k", server_id=sid))
        await s.commit()
    await authz.reload_from_db()

    pool = _FakePool()
    slots = SlotManager(
        pool, authz, unhealthy_sites=lambda s: reg.unhealthy_sites(s)
    )
    # Seed a mark for sid@cn-a, then ask for a reservation: the exclusion must flow through.
    await reg.record_outcome(sid, "upstream_timeout", site_id="cn-a")
    await reg.record_outcome(sid, "upstream_timeout", site_id="cn-a")
    payload = {
        "command": "/usr/bin/true",
        "args": [],
        "env": {},
        "vault_version": 1,
        "probe_id": "p",
        "server_id": str(sid),
        "argv": ["/usr/bin/true"],
        "trailing_allowlist": [],
    }
    with pytest.raises(PortunusError) as ei:
        await slots.reserve_and_start(sid, "k", payload)
    assert ei.value.code == "runner_unavailable"
    assert pool.picks and pool.picks[-1] == frozenset({"cn-a"})


async def test_expired_streak_rebases_to_one_failure(db, pepper_file):
    """After the TTL the streak restarts: one new timeout must not re-mark the site."""
    import os
    from datetime import datetime, timedelta, timezone

    from portunus_gateway.db.session import async_session
    from portunus_gateway.registry import Registry
    from portunus_gateway.runner_pool import RunnerPool
    from portunus_gateway.vault import Vault

    vault = Vault({1: os.urandom(32)})
    reg = Registry(async_session, vault, RunnerPool([]), trusted_path=["/usr/bin"], binary_allowlist=[])
    sid = await _mk_server(db)
    async with async_session() as s:
        s.add(
            SiteHealth(
                server_id=sid,
                site_id="cn-a",
                last_error_code="upstream_timeout",
                # Stale mark: past the TTL, streak artificially high.
                last_error_at=datetime.now(timezone.utc) - timedelta(seconds=SiteHealthPolicy.ttl_s + 60),
                consecutive_failures=99,
            )
        )
        await s.commit()
    assert await reg.unhealthy_sites(sid) == set()  # expired

    await reg.record_outcome(sid, "upstream_timeout", site_id="cn-a")
    # Streak rebased to 1 by the atomic CASE: still below threshold.
    assert await reg.unhealthy_sites(sid) == set()
    await reg.record_outcome(sid, "upstream_timeout", site_id="cn-a")
    assert await reg.unhealthy_sites(sid) == {"cn-a"}


async def test_cold_start_failure_carries_site(authz, db, pepper_file):
    """Start-phase failures attach the chosen site so the streak can move."""
    import os

    from portunus_gateway.db.session import async_session as sf
    from portunus_gateway.registry import Registry as Reg
    from portunus_gateway.runner_pool import RunnerPool, ScheduleDecision
    from portunus_gateway.slots import SlotManager
    from portunus_gateway.vault import Vault as V

    sid = await _mk_server(db)
    reg = Reg(sf, V({1: os.urandom(32)}), RunnerPool([]), trusted_path=["/usr/bin"], binary_allowlist=[])

    class _PickPool(RunnerPool):
        def pick(
            self,
            preferred_regions=(),
            *,
            loads=None,
            exclude_sites=frozenset(),
            sticky_site_id=None,
            latency_ms=None,
        ):
            return ScheduleDecision(site_id="eu", preferred_miss=False)

        async def request(self, site_id, op, **kw):
            raise PortunusError("upstream_timeout", "start ack timed out")

    from portunus_gateway.db.models import AgentKey, Grant

    async with sf() as s:
        s.add(AgentKey(key_id="k", digest=b"\x00" * 32))
        s.add(Grant(key_id="k", server_id=sid))
        await s.commit()
    await authz.reload_from_db()

    slots = SlotManager(_PickPool(), authz, unhealthy_sites=lambda s2: reg.unhealthy_sites(s2))
    payload = {
        "command": "/usr/bin/true", "args": [], "env": {}, "vault_version": 1,
        "probe_id": "p", "server_id": str(sid), "argv": ["/usr/bin/true"],
        "trailing_allowlist": [],
    }
    with pytest.raises(PortunusError) as ei:
        await slots.reserve_and_start(sid, "k", payload)
    assert ei.value.code == "upstream_timeout"
    assert getattr(ei.value, "site_id", None) == "eu"
    # The site-attributed failure feeds the learned health end to end.
    await reg.record_outcome(
        sid, "upstream_timeout", site_id=getattr(ei.value, "site_id", None)
    )
    await reg.record_outcome(sid, "upstream_timeout", site_id="eu")
    assert await reg.unhealthy_sites(sid) == {"eu"}


async def test_probe_failsover_off_partitioned_site(db, pepper_file):
    """stdio probe retries another live site when the first dies with upstream_crash."""
    import os

    from portunus_gateway.db.session import async_session as sf
    from portunus_gateway.runner_pool import RunnerPool, ScheduleDecision
    from portunus_gateway.vault import Vault as V

    sid = await _mk_server(db)

    class _SiteClient:
        connected = True
        conn_seq = 1
        site_id = "x"

        def __init__(self, site: str, *, fail: bool = False):
            self.site_id = site
            self.fail = fail
            self.calls = 0

        def start(self) -> None:
            return None

        async def request(self, op, **kw):
            from portunus_gateway.runner_client import RunnerRequestError

            self.calls += 1
            if self.fail:
                raise RunnerRequestError(
                    "upstream_crash", "child exited before answering", sent=True
                )
            return {
                "op": op,
                "ok": True,
                "tools": [{"name": "t", "inputSchema": {"type": "object"}}],
            }

    cn = _SiteClient("aliyun", fail=True)
    eu = _SiteClient("vultr", fail=False)
    picks: list[frozenset[str]] = []

    class _Pool(RunnerPool):
        def connected(self, loads=None):
            return [
                RunnerInfo(site_id="aliyun", regions=("cn-aliyun", "cn")),
                RunnerInfo(site_id="vultr", regions=("overseas-vultr", "overseas")),
            ]

        def pick(self, preferred_regions=(), *, loads=None, exclude_sites=frozenset(),
                 sticky_site_id=None, latency_ms=None):
            picks.append(frozenset(exclude_sites))
            # Prefer aliyun unless excluded (mirrors alphabetical / empty-preference bias).
            for site in ("aliyun", "vultr"):
                if site not in exclude_sites:
                    return ScheduleDecision(site_id=site, preferred_miss=False)
            return ScheduleDecision(site_id="aliyun", preferred_miss=False)

        def site_ids(self):
            return ["aliyun", "vultr"]

        async def request(self, site_id, op, **kw):
            client = cn if site_id == "aliyun" else eu
            return await client.request(op, **kw)

    import tempfile
    from pathlib import Path

    shim = Path(tempfile.mkdtemp(prefix="probe-failover-")) / "shim"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    pool = _Pool()
    reg = Registry(
        sf,
        V({1: os.urandom(32)}),
        pool,
        trusted_path=[str(shim.parent)],
        binary_allowlist=[str(shim)],
    )
    from portunus_gateway.db.models import Server

    async with sf() as s:
        server = await s.get(Server, sid)
        assert server is not None
        server.command = str(shim)
        server.args = []
        await s.commit()

    view = await reg.probe(sid, "test")
    assert any(t["name"] == "t" for t in view["tools"])
    assert cn.calls == 1 and eu.calls == 1
    # First pick had no exclusions; after aliyun crash the retry excluded it.
    assert frozenset() in picks
    assert any("aliyun" in ex for ex in picks)
    # Crash was attributed so future picks/invokes avoid aliyun (threshold=2 needs
    # one more strike — in-probe already succeeded, so streak is 1).
    assert await reg.unhealthy_sites(sid) == set()
    await reg.record_outcome(sid, "upstream_crash", site_id="aliyun")
    assert await reg.unhealthy_sites(sid) == {"aliyun"}
