"""Pure soft-affinity runner scheduler (fleet design: preference, never a hard constraint).

Given the live runners and a server's preferred regions, pick one runner:

1. candidates whose regions intersect ``preferred_regions`` win;
2. otherwise any live runner is used and the decision is flagged ``preferred_miss``;
3. no candidates at all -> ``None`` (the caller maps that to ``runner_unavailable``).

Within a tier the pick is deterministic: least ``load`` first, then ``site_id``.
No I/O and no state, so it is trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


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


def _rank(info: RunnerInfo) -> tuple[int, str]:
    return (info.load, info.site_id)


def pick_runner(
    candidates: Iterable[RunnerInfo],
    *,
    preferred_regions: Iterable[str],
    exclude_sites: frozenset[str] = frozenset(),
) -> ScheduleDecision | None:
    """Soft-affinity pick with learned-health exclusion.

    ``exclude_sites`` names runner sites whose network path to this server recently
    failed (auto-failover): they are dropped before the preference tiers, so an
    operator's region pin is only ever honored among sites that can actually reach
    the upstream — the pin degrades to a hint instead of pinning into a partition.
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
    wanted = frozenset(preferred_regions)
    if wanted:
        matching = [c for c in pool if wanted.intersection(c.regions)]
        if matching:
            return ScheduleDecision(site_id=min(matching, key=_rank).site_id, preferred_miss=False)
        return ScheduleDecision(site_id=min(pool, key=_rank).site_id, preferred_miss=True)
    return ScheduleDecision(site_id=min(pool, key=_rank).site_id, preferred_miss=False)
