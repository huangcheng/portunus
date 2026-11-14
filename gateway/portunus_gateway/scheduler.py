"""Pure soft-affinity runner scheduler (fleet design: preference, never a hard constraint).

Given the live runners and a server's preferred regions, pick one runner:

1. ``sticky_site_id`` (skill-bridge login site) hard-pins when that site is live;
2. candidates whose regions intersect ``preferred_regions`` win;
3. otherwise any live runner is used and the decision is flagged ``preferred_miss``;
4. no candidates at all -> ``None`` (the caller maps that to ``runner_unavailable``).

Within a tier the pick ranks by EWMA upstream latency (when known), then least ``load``,
then ``site_id``. Unsampled sites use ``LATENCY_UNKNOWN_MS`` so they still get tried.
No I/O and no state, so it is trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Mid default for sites with no successful samples yet (cold explore + load balance).
LATENCY_UNKNOWN_MS = 500.0


@dataclass(frozen=True, slots=True)
class RunnerInfo:
    """What the scheduler needs to know about one live runner."""

    site_id: str
    regions: tuple[str, ...] = ("local",)
    load: int = 0  # e.g. current child count; lower is preferred


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    site_id: str
    preferred_miss: bool  # True when no live runner matched a non-empty preference


def _rank(
    info: RunnerInfo,
    *,
    latency_ms: Mapping[str, float],
) -> tuple[float, int, str]:
    lat = latency_ms.get(info.site_id)
    if lat is None:
        lat = LATENCY_UNKNOWN_MS
    return (float(lat), info.load, info.site_id)


def pick_runner(
    candidates: Iterable[RunnerInfo],
    *,
    preferred_regions: Iterable[str],
    exclude_sites: frozenset[str] = frozenset(),
    sticky_site_id: str | None = None,
    latency_ms: Mapping[str, float] | None = None,
) -> ScheduleDecision | None:
    """Soft-affinity pick with learned-health exclusion and optional sticky / latency.

    ``exclude_sites`` names runner sites whose network path to this server recently
    failed (auto-failover): they are dropped before the preference tiers, so an
    operator's region pin is only ever honored among sites that can actually reach
    the upstream — the pin degrades to a hint instead of pinning into a partition.

    ``sticky_site_id`` (skill session / preferred_site_id) is a **hard** pin: if set,
    that site must be live and not excluded, else ``None`` (no silent fallback to an
    empty-state runner). Latency ranking never overrides a sticky pin.
    """
    # Materialize once: the fallback below must reuse the same candidates.
    all_candidates = list(candidates)
    pool = [c for c in all_candidates if c.site_id not in exclude_sites]
    if not pool:
        # Every live site is marked unhealthy for this server: a partition beats a
        # hard refusal, so fall back to trying them anyway (the mark expires by TTL).
        pool = all_candidates
        if not pool:
            return None

    lat = latency_ms or {}
    sticky = (sticky_site_id or "").strip() or None
    if sticky is not None:
        for c in pool:
            if c.site_id == sticky:
                return ScheduleDecision(site_id=sticky, preferred_miss=False)
        # Sticky site missing from the (possibly unrebased) pool: hard refuse.
        return None

    wanted = frozenset(preferred_regions)
    if wanted:
        matching = [c for c in pool if wanted.intersection(c.regions)]
        if matching:
            return ScheduleDecision(
                site_id=min(matching, key=lambda i: _rank(i, latency_ms=lat)).site_id,
                preferred_miss=False,
            )
        return ScheduleDecision(
            site_id=min(pool, key=lambda i: _rank(i, latency_ms=lat)).site_id,
            preferred_miss=True,
        )
    return ScheduleDecision(
        site_id=min(pool, key=lambda i: _rank(i, latency_ms=lat)).site_id,
        preferred_miss=False,
    )
