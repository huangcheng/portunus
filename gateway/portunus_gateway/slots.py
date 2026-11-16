"""Child slot FSM (spec §6.1): reserve -> start -> commit, caps, pending_stop, reconcile.

Invariants
----------
* One invoke child per ``(server_id, key_id)`` (per-pair mutex); caps 32/key, 256 global.
  Every slot -- reserved, committed or pending_stop -- counts against the caps.
* ``reserve -> start (child_id minted here) -> commit``. A start the runner *refused* (or that
  never left the gateway) is rolled back at once. Anything else is freed **only on exit
  evidence**: ``child_exited``, a reconcile inventory that no longer lists it, or the runner
  answering a ``kill`` with ``unknown_child``. There is no force-free.
* ``pending_stop`` is sticky: it survives reconnects, a child that is pending_stop is never
  adopted or committed, and the kill is retried until exit evidence (or the socket drops, in
  which case the next reconcile re-issues it).
* Every slot is pinned to the ``site_id`` of the runner it was started on (fleet: the pick is
  made once, by the scheduler, at reserve time). Stop/kill/rpc/reconcile for a slot only ever
  talk to that site's connection; one site's inventory or ``child_exited`` is never evidence
  about another site's children.
* Authorization is re-checked at commit (``is_revoked`` first, because
  ``AuthzStore.has_grant`` keeps answering for revoked keys) and again for every child the
  runner lists on reconnect. A start that races a revoke/ungrant can therefore never come
  back live to its caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

from portunus_shared import DEFAULTS, PortunusError

from .runner_client import RunnerRequestError
from .runner_pool import RunnerPool

log = logging.getLogger("portunus.gateway.slots")


class ReserveResult(NamedTuple):
    """Outcome of ``reserve_and_start``: live child id and whether this call started it."""

    child_id: str
    cold_start: bool


class _Authz(Protocol):
    generation: int

    def is_revoked(self, key_id: str) -> bool: ...

    def has_grant(self, key_id: str, server_id: uuid.UUID) -> bool: ...


class _Client(Protocol):
    connected: bool
    conn_seq: int

    async def request(
        self, op: str, *, timeout: float, conn_seq: int | None = None, **fields: Any
    ) -> dict: ...


class SlotState(enum.Enum):
    RESERVED = "reserved"  # start sent (or unresolved); not yet known to be live
    COMMITTED = "committed"  # runner acked and authz still allowed it


@dataclass(eq=False)
class Slot:
    child_id: str
    server_id: uuid.UUID | None
    key_id: str | None  # None: a child the gateway has no record of (adopted only to be killed)
    state: SlotState
    sent_seq: int  # runner connection generation the start was issued on
    generation: int
    vault_version: int | None
    pending_stop: bool = False
    site_id: str = "local"  # runner site that owns the child; every request for it goes there
    preferred_miss: bool = False  # scheduled outside the server's preferred regions
    exited: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    kill_attempted: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_task: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class ReconcileResult:
    kept: list[str] = field(default_factory=list)  # adopted / confirmed live and allowed
    stopped: list[str] = field(default_factory=list)  # pending_stop; kill issued
    freed: list[str] = field(default_factory=list)  # absent from inventory


def _str_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise PortunusError("spawn_rejected", f"{what} must be a list of strings")
    return list(value)


class SlotManager:
    def __init__(
        self,
        client: _Client | RunnerPool,
        authz: _Authz,
        *,
        max_children_per_key: int = DEFAULTS.max_children_per_key,
        max_children_global: int = DEFAULTS.max_children_global,
        start_ack_s: float = DEFAULTS.start_ack_s,
        kill_ack_s: float = 5.0,
        stop_retry_s: float = 1.0,
        server_enabled: Callable[[uuid.UUID], bool] | None = None,
        vault_version: Callable[[uuid.UUID], int | None] | None = None,
        unhealthy_sites: Callable[[uuid.UUID], Awaitable[Iterable[str]]] | None = None,
        site_latencies: Callable[[uuid.UUID], Awaitable[Mapping[str, float]]] | None = None,
    ) -> None:
        self.client = client  # a RunnerClient or a RunnerPool; routing always goes via the pool
        self.pool = RunnerPool.ensure(client)
        self.authz = authz
        self.max_children_per_key = max_children_per_key
        self.max_children_global = max_children_global
        self.start_ack_s = start_ack_s
        self.kill_ack_s = kill_ack_s
        self.stop_retry_s = stop_retry_s
        # Learned per-(server, site) network health (registry): auto-failover feed.
        self.unhealthy_sites = unhealthy_sites
        self._exclusion_cache: dict[uuid.UUID, tuple[float, frozenset[str], bool]] = {}
        self._exclusion_cache_s = 5.0
        # EWMA upstream_ms per site for latency-aware cold starts (same short TTL).
        self.site_latencies = site_latencies
        self._latency_cache: dict[uuid.UUID, tuple[float, dict[str, float]]] = {}
        self._latency_cache_s = 5.0
        self.server_enabled = server_enabled
        self.vault_version = vault_version
        self._slots: dict[str, Slot] = {}
        self._by_pair: dict[tuple[uuid.UUID, str], Slot] = {}
        self._pair_locks: dict[tuple[uuid.UUID, str], asyncio.Lock] = {}
        self._foreign_kills: set[asyncio.Task] = set()

    # ---- wiring / introspection --------------------------------------------------

    def attach(self, target: Any) -> None:
        """Hook a RunnerClient (or every client of a pool) so reconnects reconcile that site and
        its ``child_exited`` frames free that site's slots."""
        if isinstance(target, RunnerPool):
            for c in target.clients:
                self.attach(c)
            return
        client = target

        def site() -> str | None:
            return self.pool.site_of(client)

        async def on_connected() -> None:
            await self.sync_with_runner(site())

        def on_exited(child_id: str) -> bool:
            return self.on_child_exited(child_id, site_id=site())

        client.on_connected = on_connected
        client.on_child_exited = on_exited

    def _site_or_only(self, site_id: str | None) -> str:
        """``site_id``, or the pool's single site (v1 callers never name one)."""
        if site_id is not None:
            return site_id
        ids = self.pool.site_ids()
        if len(ids) != 1:
            raise ValueError("site_id is required with a multi-runner pool")
        return ids[0]

    def site_of(self, child_id: str) -> str | None:
        slot = self._slots.get(child_id)
        return slot.site_id if slot is not None else None

    async def _site_exclusions(self, server_id: uuid.UUID) -> tuple[frozenset[str], bool]:
        """``(excluded_sites, every_live_site_marked)`` with a short in-memory cache.

        The learned look-up is a DB round trip and ``reserve_and_start`` is the invoke
        hot path (inside the per-pair lock), so answers are cached briefly — marks are
        TTL-based anyway, and a few seconds of staleness cannot strand a child: the
        failure that set the mark already retired the pair through its own error path.
        """
        now = time.monotonic()
        hit = self._exclusion_cache.get(server_id)
        if hit is not None and now - hit[0] < self._exclusion_cache_s:
            return hit[1], hit[2]
        excluded: frozenset[str] = frozenset()
        if self.unhealthy_sites is not None:
            try:
                excluded = frozenset(await self.unhealthy_sites(server_id))
            except Exception:  # noqa: BLE001 — placement must never fail on health lookups
                log.exception("unhealthy_sites lookup failed; placing unfiltered")
        live = {info.site_id for info in self.pool.connected()}
        all_marked = bool(live) and live <= set(excluded)
        self._exclusion_cache[server_id] = (now, excluded, all_marked)
        return excluded, all_marked

    async def _site_latency_map(self, server_id: uuid.UUID) -> dict[str, float]:
        """Cached EWMA map for cold-start ranking; empty when lookup is unavailable."""
        now = time.monotonic()
        hit = self._latency_cache.get(server_id)
        if hit is not None and now - hit[0] < self._latency_cache_s:
            return dict(hit[1])
        out: dict[str, float] = {}
        if self.site_latencies is not None:
            try:
                raw = await self.site_latencies(server_id)
                out = {str(k): float(v) for k, v in dict(raw).items()}
            except Exception:  # noqa: BLE001 — placement must never fail on latency lookups
                log.exception("site_latencies lookup failed; placing without EWMA")
        self._latency_cache[server_id] = (now, out)
        return dict(out)

    def _loads(self) -> dict[str, int]:
        loads: dict[str, int] = {}
        for s in self._slots.values():
            loads[s.site_id] = loads.get(s.site_id, 0) + 1
        return loads

    @property
    def slots(self) -> dict[str, Slot]:
        return dict(self._slots)

    def slot(self, child_id: str) -> Slot | None:
        return self._slots.get(child_id)

    def committed_for_key(self, key_id: str) -> list[Slot]:
        return [
            s
            for s in self._slots.values()
            if s.key_id == key_id and s.state is SlotState.COMMITTED and not s.pending_stop
        ]

    # ---- authz predicates --------------------------------------------------------

    def _deny_reason(
        self,
        key_id: str,
        server_id: uuid.UUID | None,
        *,
        is_revoked: Callable[[str], bool] | None = None,
        has_grant: Callable[[str, uuid.UUID], bool] | None = None,
        server_enabled: Callable[[uuid.UUID], bool] | None = None,
    ) -> str | None:
        """Error code if (key, server) may not hold a child, else None.

        ``is_revoked`` is consulted BEFORE ``has_grant``: grants of a revoked key linger.
        """
        is_revoked = is_revoked or self.authz.is_revoked
        has_grant = has_grant or self.authz.has_grant
        server_enabled = server_enabled or self.server_enabled
        if is_revoked(key_id):
            return "revoked"
        if server_id is None or not has_grant(key_id, server_id):
            return "not_granted"
        if server_enabled is not None and not server_enabled(server_id):
            return "server_disabled"
        return None

    def _admit(self, key_id: str, server_id: uuid.UUID) -> None:
        reason = self._deny_reason(key_id, server_id)
        if reason is not None:
            raise PortunusError(reason)

    def _vault_stale(
        self,
        slot: Slot,
        vault_version: Callable[[uuid.UUID], int | None] | None = None,
    ) -> bool:
        fn = vault_version or self.vault_version
        if fn is None or slot.server_id is None or slot.vault_version is None:
            return False
        cur = fn(slot.server_id)
        return cur is not None and cur != slot.vault_version

    # ---- reserve / start / commit -----------------------------------------------

    @staticmethod
    def _validate_payload(p: Any) -> tuple[list[str], list[str], dict[str, str], int | None]:
        if not isinstance(p, dict):
            raise PortunusError("spawn_rejected", "start payload must be an object")
        argv = _str_list(p.get("argv"), "argv")
        if not argv:
            raise PortunusError("spawn_rejected", "empty argv")
        trailing = _str_list(p.get("trailing_allowlist", []), "trailing_allowlist")
        env = p.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise PortunusError("spawn_rejected", "env must map strings to strings")
        vv = p.get("vault_version")
        if vv is not None and (isinstance(vv, bool) or not isinstance(vv, int)):
            raise PortunusError("spawn_rejected", "vault_version must be an int")
        return argv, trailing, dict(env), vv

    def _check_caps(self, key_id: str) -> None:
        if len(self._slots) >= self.max_children_global:
            raise PortunusError("child_cap", "global child cap reached")
        if sum(1 for s in self._slots.values() if s.key_id == key_id) >= self.max_children_per_key:
            raise PortunusError("child_cap", "per-key child cap reached")

    async def reserve_and_start(
        self,
        server_id: uuid.UUID,
        key_id: str,
        start_payload: dict,
        *,
        preferred_regions: Iterable[str] = (),
    ) -> ReserveResult:
        """Return a live, authorized child for the pair (reusing one when possible).

        ``cold_start`` is False when an existing committed child is reused; True when this call
        starts a new child.

        Raises ``revoked`` / ``not_granted`` / ``server_disabled`` (admission or lost a race),
        ``child_cap``, ``runner_unavailable`` (no live runner in the pool), ``spawn_rejected`` /
        ``spawn_failed`` (runner refused), ``upstream_timeout`` (no ack in ``start_ack_s``; the child is pending_stop),
        ``upstream_crash`` (exited before the ack) or ``cancelled`` (stopped mid-start).
        """
        argv, trailing, env, vault_version = self._validate_payload(start_payload)
        sticky_raw = start_payload.get("sticky_site_id")
        sticky_site_id = sticky_raw.strip() if isinstance(sticky_raw, str) else None
        if sticky_site_id == "":
            sticky_site_id = None
        self._admit(key_id, server_id)
        pair = (server_id, key_id)
        lock = self._pair_locks.setdefault(pair, asyncio.Lock())
        async with lock:
            self._admit(key_id, server_id)  # state may have changed while queued
            excluded, all_marked = await self._site_exclusions(server_id)
            existing = self._by_pair.get(pair)
            if existing is not None:
                if existing.state is SlotState.COMMITTED and not existing.pending_stop:
                    if existing.site_id in excluded and not all_marked:
                        # Healthy-looking child on a site we learned is partitioned for
                        # this server: retire it and re-place on the next invoke rather
                        # than reusing into the same black hole. When every live site
                        # is marked we keep reusing — the scheduler's "a partition
                        # beats a hard refusal" fallback.
                        self._mark_pending_stop(existing)
                        raise PortunusError(
                            "runner_unavailable",
                            "child on a network-marked site; re-placing",
                        )
                    elif not self._vault_stale(existing):
                        return ReserveResult(existing.child_id, cold_start=False)
                    else:
                        self._mark_pending_stop(existing)
                # Unresolved start or a child still being stopped: never double-spawn.
                raise PortunusError(
                    "runner_unavailable", "previous child for this server/key still resolving"
                )
            latency = await self._site_latency_map(server_id)
            decision = self.pool.pick(
                preferred_regions,
                loads=self._loads(),
                exclude_sites=frozenset() if all_marked else excluded,
                sticky_site_id=sticky_site_id,
                latency_ms=latency,
            )
            if decision is None:
                raise PortunusError("runner_unavailable", "runner not connected")
            self._check_caps(key_id)  # reserve is synchronous with the check: no overshoot
            slot = Slot(
                child_id=uuid.uuid4().hex,
                server_id=server_id,
                key_id=key_id,
                state=SlotState.RESERVED,
                sent_seq=self.pool.conn_seq(decision.site_id),
                generation=self.authz.generation,
                vault_version=vault_version,
                site_id=decision.site_id,
                preferred_miss=decision.preferred_miss,
            )
            self._slots[slot.child_id] = slot
            self._by_pair[pair] = slot
            # Ids only: never argv/env. Misses are worth an operator's attention.
            log.log(
                logging.INFO if slot.preferred_miss else logging.DEBUG,
                "scheduled child server=%s site=%s preferred_miss=%s",
                server_id,
                slot.site_id,
                slot.preferred_miss,
            )
            try:
                child_id = await self._start(slot, argv, trailing, env)
            except PortunusError as e:
                # Cold-start failures (start-ack timeout, link loss) must still name the
                # hosting site so the learned health can mark it (see registry).
                if getattr(e, "site_id", None) is None:
                    e.site_id = slot.site_id  # type: ignore[attr-defined]
                raise
            return ReserveResult(child_id, cold_start=True)

    async def _start(
        self, slot: Slot, argv: list[str], trailing: list[str], env: dict[str, str]
    ) -> str:
        assert slot.key_id is not None and slot.server_id is not None
        # Explicit start fields (the wire.start() helper cannot carry command/env on its own):
        # argv[0] is the command; env is literals only.
        fields = {
            "child_id": slot.child_id,
            "server_id": str(slot.server_id),
            "argv": argv,
            "trailing_allowlist": trailing,
            "env": env,
        }
        try:
            await self.pool.request(
                slot.site_id, "start", timeout=self.start_ack_s, conn_seq=slot.sent_seq, **fields
            )
        except asyncio.CancelledError:
            # Caller went away mid-start: the child may exist. Stop it; free on evidence.
            if self._slots.get(slot.child_id) is slot:
                self._mark_pending_stop(slot)
            raise
        except RunnerRequestError as e:
            if not e.sent or e.reply is not None:
                self._free(slot)  # never reached the runner / runner refused: rollback
            elif e.code == "upstream_timeout":
                # No ack: it may or may not run. pending_stop survives reconnect; kill if up.
                self._mark_pending_stop(slot)
            elif self._deny_reason(slot.key_id, slot.server_id) is not None:
                slot.pending_stop = True  # connection lost mid-start AND no longer allowed
            # else: unresolved RESERVED; reconcile adopts (listed+allowed) or frees (absent)
            raise

        if self._slots.get(slot.child_id) is not slot:
            raise PortunusError(
                "upstream_crash",
                "child exited before start completed",
                child_exited=True,
            )
        # Commit gate: authz may have changed while the start was in flight. No await from here
        # to the return, so nothing can interleave between this check and the commit.
        reason = self._deny_reason(slot.key_id, slot.server_id)
        if reason is None and self._vault_stale(slot):
            reason = "cancelled"
        if reason is None and slot.pending_stop:
            reason = "cancelled"
        if reason is not None:
            self._mark_pending_stop(slot)
            raise PortunusError(reason, "authorization changed during start")
        slot.state = SlotState.COMMITTED
        slot.generation = self.authz.generation  # re-stamp: validated against current state
        return slot.child_id

    # ---- exit evidence -----------------------------------------------------------

    def on_child_exited(self, child_id: str, site_id: str | None = None) -> bool:
        """``child_exited`` from a runner: exit evidence, frees the slot.

        With ``site_id`` the frame only counts if that site owns the slot.
        """
        slot = self._slots.get(child_id)
        if slot is None or (site_id is not None and slot.site_id != site_id):
            return False
        self._free(slot)
        return True

    def _free(self, slot: Slot) -> None:
        if self._slots.get(slot.child_id) is slot:
            del self._slots[slot.child_id]
        if slot.key_id is not None and slot.server_id is not None:
            pair = (slot.server_id, slot.key_id)
            if self._by_pair.get(pair) is slot:
                del self._by_pair[pair]
        slot.exited.set()

    # ---- pending_stop ------------------------------------------------------------

    def _mark_pending_stop(self, slot: Slot) -> None:
        slot.pending_stop = True
        if self._slots.get(slot.child_id) is slot:
            self._ensure_stop(slot)

    def _ensure_stop(self, slot: Slot) -> None:
        if slot.stop_task is not None and not slot.stop_task.done():
            return
        slot.kill_attempted = asyncio.Event()
        slot.stop_task = asyncio.get_running_loop().create_task(
            self._stop_loop(slot, slot.kill_attempted)
        )

    async def _stop_loop(self, slot: Slot, attempted: asyncio.Event) -> None:
        """Kill until exit evidence. Gives up (without freeing) when the socket is down:
        reconcile re-issues the kill after reconnect."""
        try:
            while self._slots.get(slot.child_id) is slot:
                if not self.pool.is_connected(slot.site_id):
                    return
                try:
                    await self.pool.request(
                        slot.site_id, "kill", timeout=self.kill_ack_s, child_id=slot.child_id
                    )
                except RunnerRequestError as e:
                    if e.reason == "unknown_child":
                        self._free(slot)  # runner has no such child: same as inventory absence
                        return
                    if e.code == "runner_unavailable":
                        return
                    # timeout / other error: retry below
                finally:
                    attempted.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(slot.exited.wait(), self.stop_retry_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stop loop failed for %s", slot.child_id)
        finally:
            attempted.set()

    def _stop_matching(self, pred: Callable[[Slot], bool]) -> list[Slot]:
        hit = [s for s in self._slots.values() if pred(s)]
        for s in hit:
            self._mark_pending_stop(s)
        return hit

    def revoke_key(self, key_id: str) -> None:
        """Key revoked: every child of the key (committed or mid-start) -> pending_stop."""
        self._stop_matching(lambda s: s.key_id == key_id)

    def ungrant(self, key_id: str, server_id: uuid.UUID) -> None:
        self._stop_matching(lambda s: s.key_id == key_id and s.server_id == server_id)

    def stop_pair(self, key_id: str, server_id: uuid.UUID) -> None:
        """Retire one ``(key, server)`` child (e.g. wedged in a timed-out call); no authz change."""
        self._stop_matching(lambda s: s.key_id == key_id and s.server_id == server_id)

    def disable_server(self, server_id: uuid.UUID) -> None:
        """Server disabled: same stop path as ungrant, for every key."""
        self._stop_matching(lambda s: s.server_id == server_id)

    # ---- reconcile ---------------------------------------------------------------

    async def sync_with_runner(self, site_id: str | None = None) -> ReconcileResult:
        """Request inventory on a site's current connection and reconcile against it."""
        site = self._site_or_only(site_id)
        seq = self.pool.conn_seq(site)
        reply = await self.pool.request(
            site, "inventory", timeout=self.kill_ack_s * 2, conn_seq=seq
        )
        children = reply.get("children")
        if not isinstance(children, list):
            raise PortunusError("unsupported", "malformed inventory")
        return await self.reconcile(children, conn_seq=seq, site_id=site)

    async def reconcile(
        self,
        inventory: list[dict],
        *,
        is_revoked: Callable[[str], bool] | None = None,
        has_grant: Callable[[str, uuid.UUID], bool] | None = None,
        generation: Callable[[], int] | None = None,
        server_enabled: Callable[[uuid.UUID], bool] | None = None,
        vault_version: Callable[[uuid.UUID], int | None] | None = None,
        conn_seq: int | None = None,
        site_id: str | None = None,
    ) -> ReconcileResult:
        """Reconcile one site's slots with that runner's complete inventory (spec §6.1 grace
        step 3). Slots owned by other sites are never freed, adopted or stopped by it, and a
        listed child that another site owns is killed on the reporting site, not adopted.

        Predicates default to this manager's authz store. A listed child is kept/adopted only
        if its key is not revoked, the server is granted + enabled, it is not pending_stop and
        its ``vault_version`` matches; otherwise it is marked pending_stop and killed. Absent
        slots are freed (inventory absence is exit evidence). Unknown listed children are
        killed. ``conn_seq`` is the connection the inventory was taken on: slots whose start
        was issued on that connection (possibly after the inventory snapshot) are neither
        freed for absence nor adopted -- their own ack decides.
        """
        gen_now = generation() if generation is not None else self.authz.generation
        site = self._site_or_only(site_id)
        seq = self.pool.conn_seq(site) if conn_seq is None else conn_seq
        listed: dict[str, dict] = {}
        for item in inventory:
            cid = item.get("child_id") if isinstance(item, dict) else None
            if isinstance(cid, str) and cid:
                listed[cid] = item

        res = ReconcileResult()
        for slot in list(self._slots.values()):
            if slot.site_id != site or slot.child_id in listed or slot.sent_seq >= seq:
                continue
            self._free(slot)
            res.freed.append(slot.child_id)

        waits: list[asyncio.Event] = []
        for cid, item in listed.items():
            slot = self._slots.get(cid)
            if slot is not None and slot.site_id != site:
                # Owned by another site: this inventory is no evidence about it.
                self._kill_foreign(site, cid)
                res.stopped.append(cid)
                continue
            if slot is None:
                slot = self._track_unknown(cid, item, seq, gen_now, site)
                res.stopped.append(cid)
                waits.append(slot.kill_attempted)
                continue
            if slot.key_id is None:
                # Ghost (unknown to the gateway) left over from an earlier reconcile whose
                # kill never produced exit evidence: keep killing it, never adopt it.
                self._mark_pending_stop(slot)
                res.stopped.append(cid)
                waits.append(slot.kill_attempted)
                continue
            deny = self._deny_reason(
                slot.key_id,
                slot.server_id,
                is_revoked=is_revoked,
                has_grant=has_grant,
                server_enabled=server_enabled,
            )
            if deny is not None or slot.pending_stop or self._vault_stale(slot, vault_version):
                self._mark_pending_stop(slot)
                res.stopped.append(cid)
                waits.append(slot.kill_attempted)
                continue
            if slot.state is SlotState.RESERVED:
                if slot.sent_seq >= seq:
                    continue  # in flight on this connection: its own ack/commit gate decides
                slot.state = SlotState.COMMITTED  # unresolved start survived: adopt
            slot.generation = gen_now
            res.kept.append(cid)

        if waits:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.kill_ack_s + 1):
                    await asyncio.gather(*(e.wait() for e in waits))
        return res

    def _kill_foreign(self, site: str, child_id: str) -> None:
        """Best-effort kill of a duplicate child on ``site`` that another site's slot owns."""

        async def run() -> None:
            with contextlib.suppress(RunnerRequestError):
                await self.pool.request(
                    site, "kill", timeout=self.kill_ack_s, child_id=child_id
                )

        task = asyncio.get_running_loop().create_task(run())
        self._foreign_kills.add(task)
        task.add_done_callback(self._foreign_kills.discard)

    def _track_unknown(
        self, child_id: str, item: dict, seq: int, gen: int, site: str
    ) -> Slot:
        sid: uuid.UUID | None
        try:
            sid = uuid.UUID(str(item.get("server_id")))
        except ValueError:
            sid = None
        slot = Slot(
            child_id=child_id,
            server_id=sid,
            key_id=None,
            state=SlotState.COMMITTED,
            sent_seq=seq,
            generation=gen,
            vault_version=None,
            pending_stop=True,
            site_id=site,
        )
        self._slots[child_id] = slot
        self._mark_pending_stop(slot)
        return slot
