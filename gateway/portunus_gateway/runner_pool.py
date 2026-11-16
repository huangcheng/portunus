"""Pool of runner links keyed by site (fleet design).

A ``RunnerPool`` owns N ``RunnerClient``-shaped links (anything with ``connected``,
``conn_seq`` and ``request``; ``site_id`` / ``regions`` are optional and default to the v1
single local site). It answers three questions for the control plane:

* **who is live** -- ``connected()`` -> ``RunnerInfo`` (site, regions, load) for scheduling;
* **where should new work go** -- ``pick(preferred_regions)`` via the pure scheduler;
* **how do I reach one site** -- ``request(site_id, op, ...)`` goes to that site's link only.

A child lives on exactly one site for its whole life; callers record the ``site_id`` when they
start it and address every later request (rpc/kill/inventory) to that same site.

Site keys: ``add(client, site_id=...)`` pins the key; ``add(client)`` leaves it unpinned so it
tracks whatever the runner advertised on hello (``client.site_id``). The latter is what the
single-runner bootstrap uses, so a v1 runner (no metadata) is simply the site ``"local"``.

``RunnerPool.ensure(x)`` wraps a bare client in a pool of one (or passes a pool through), which
keeps single-client callers and tests working unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .runner_client import DEFAULT_SITE_ID, RunnerRequestError
from .scheduler import RunnerInfo, ScheduleDecision, pick_runner

log = logging.getLogger("portunus.gateway.pool")


@dataclass(eq=False)
class _Entry:
    client: Any
    pinned: str | None  # explicit site key, or None to follow the client's advertised site

    @property
    def site_id(self) -> str:
        if self.pinned is not None:
            return self.pinned
        site = getattr(self.client, "site_id", None)
        return site if isinstance(site, str) and site else DEFAULT_SITE_ID

    @property
    def regions(self) -> tuple[str, ...]:
        raw = getattr(self.client, "regions", None)
        regions = tuple(r for r in raw if isinstance(r, str) and r) if raw else ()
        return regions or (DEFAULT_SITE_ID,)

    @property
    def connected(self) -> bool:
        return bool(getattr(self.client, "connected", False))


class RunnerPool:
    def __init__(self, clients: Iterable[Any] = ()) -> None:
        self._entries: list[_Entry] = []
        for c in clients:
            self.add(c)

    @classmethod
    def ensure(cls, runner: Any) -> RunnerPool:
        """``runner`` itself if it is a pool, else a pool of one wrapping that client."""
        return runner if isinstance(runner, RunnerPool) else cls([runner])

    # ---- membership --------------------------------------------------------------

    def add(self, client: Any, *, site_id: str | None = None) -> None:
        if any(e.client is client for e in self._entries):
            raise ValueError("client already in pool")
        if site_id is not None and any(e.site_id == site_id for e in self._entries):
            raise ValueError(f"site {site_id!r} already in pool")
        self._entries.append(_Entry(client, site_id))

    def remove(self, site_id: str) -> Any | None:
        """Drop a site's link from the pool and return it (the caller closes it)."""
        entry = self._entry(site_id)
        if entry is None:
            return None
        self._entries.remove(entry)
        return entry.client

    @property
    def clients(self) -> list[Any]:
        return [e.client for e in self._entries]

    def __len__(self) -> int:
        return len(self._entries)

    def site_ids(self) -> list[str]:
        return [e.site_id for e in self._entries]

    def site_of(self, client: Any) -> str | None:
        """Current site key of a pooled client (None if it is not in the pool)."""
        for e in self._entries:
            if e.client is client:
                return e.site_id
        return None

    def _entry(self, site_id: str) -> _Entry | None:
        for e in self._entries:  # first match wins if two unpinned clients advertise one site
            if e.site_id == site_id:
                return e
        return None

    # ---- lifecycle (clients that have none are left alone) -----------------------

    def start(self) -> None:
        for e in self._entries:
            fn = getattr(e.client, "start", None)
            if callable(fn):
                fn()

    async def close(self) -> None:
        entries = list(self._entries)
        self._entries.clear()
        for e in entries:
            fn = getattr(e.client, "close", None)
            if not callable(fn):
                continue
            try:
                await fn()
            except Exception:
                log.exception("runner client close failed")

    # ---- liveness / scheduling ---------------------------------------------------

    def is_connected(self, site_id: str) -> bool:
        entry = self._entry(site_id)
        return entry is not None and entry.connected

    def any_connected(self) -> bool:
        return any(e.connected for e in self._entries)

    def conn_seq(self, site_id: str) -> int:
        """Connection generation of a site's link (0 if the site is unknown)."""
        entry = self._entry(site_id)
        return int(getattr(entry.client, "conn_seq", 0)) if entry is not None else 0

    def connected(self, loads: Mapping[str, int] | None = None) -> list[RunnerInfo]:
        """Live runners as scheduler input. ``loads`` maps site -> current child count."""
        loads = loads or {}
        out: list[RunnerInfo] = []
        seen: set[str] = set()
        for e in self._entries:
            if not e.connected:
                continue
            site = e.site_id
            if site in seen:
                log.warning("two live runners advertise site %r; ignoring the later one", site)
                continue
            seen.add(site)
            out.append(RunnerInfo(site_id=site, regions=e.regions, load=loads.get(site, 0)))
        return out

    def pick(
        self,
        preferred_regions: Iterable[str] = (),
        *,
        loads: Mapping[str, int] | None = None,
        exclude_sites: frozenset[str] = frozenset(),
        sticky_site_id: str | None = None,
        latency_ms: Mapping[str, float] | None = None,
    ) -> ScheduleDecision | None:
        """Soft-affinity pick over live runners; ``None`` means no runner (-> runner_unavailable)."""
        return pick_runner(
            self.connected(loads),
            preferred_regions=preferred_regions,
            exclude_sites=exclude_sites,
            sticky_site_id=sticky_site_id,
            latency_ms=latency_ms,
        )

    # ---- routing -----------------------------------------------------------------

    async def request(
        self,
        site_id: str,
        op: str,
        *,
        timeout: float,
        conn_seq: int | None = None,
        **fields: Any,
    ) -> dict:
        """Send ``op`` on exactly ``site_id``'s link. Never falls back to another site."""
        entry = self._entry(site_id)
        if entry is None or not entry.connected:
            raise RunnerRequestError(
                "runner_unavailable", f"runner for site {site_id!r} not connected", sent=False
            )
        return await entry.client.request(op, timeout=timeout, conn_seq=conn_seq, **fields)
