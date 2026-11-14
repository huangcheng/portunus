"""Pure soft-affinity scheduler: preferred regions first, any live runner as fallback."""

from __future__ import annotations

import itertools

from portunus_gateway.scheduler import (
    LATENCY_UNKNOWN_MS,
    RunnerInfo,
    ScheduleDecision,
    pick_runner,
)


def r(site_id: str, regions: list[str], load: int = 0) -> RunnerInfo:
    return RunnerInfo(site_id=site_id, regions=tuple(regions), load=load)


def test_sticky_hard_pins_live_site():
    cands = [r("slow", ["local"], 0), r("pin", ["local"], 9)]
    d = pick_runner(cands, preferred_regions=[], sticky_site_id="pin", latency_ms={"slow": 1})
    assert d == ScheduleDecision(site_id="pin", preferred_miss=False)


def test_sticky_absent_returns_none():
    assert pick_runner([r("a", ["local"])], preferred_regions=[], sticky_site_id="gone") is None


def test_latency_beats_load_without_preference():
    cands = [r("fast", ["local"], 5), r("slow", ["local"], 0)]
    d = pick_runner(
        cands, preferred_regions=[], latency_ms={"fast": 20.0, "slow": 200.0}
    )
    assert d is not None and d.site_id == "fast"


def test_preferred_tier_before_latency():
    cands = [r("eu", ["eu"], 0), r("cn", ["cn"], 0)]
    d = pick_runner(
        cands,
        preferred_regions=["cn"],
        latency_ms={"eu": 5.0, "cn": 500.0},
    )
    assert d == ScheduleDecision(site_id="cn", preferred_miss=False)


def test_unknown_latency_uses_mid_default():
    cands = [r("known", ["local"]), r("new", ["local"])]
    d = pick_runner(
        cands,
        preferred_regions=[],
        latency_ms={"known": LATENCY_UNKNOWN_MS + 1},
    )
    assert d is not None and d.site_id == "new"


def test_empty_candidates_returns_none():
    assert pick_runner([], preferred_regions=["region-a"]) is None
    assert pick_runner([], preferred_regions=[]) is None


def test_prefers_region_match_over_less_loaded_non_match():
    cands = [r("site-b", ["region-b"], load=0), r("site-a", ["region-a"], load=9)]
    d = pick_runner(cands, preferred_regions=["region-a"])
    assert d == ScheduleDecision(site_id="site-a", preferred_miss=False)


def test_any_overlap_in_multi_region_runner_matches():
    cands = [r("site-a", ["region-a", "region-b"]), r("site-l", ["local"])]
    d = pick_runner(cands, preferred_regions=["region-b", "region-x"])
    assert d is not None and d.site_id == "site-a" and not d.preferred_miss


def test_falls_back_to_any_live_runner_with_preferred_miss():
    cands = [r("site-b", ["region-b"], load=3), r("site-l", ["local"], load=1)]
    d = pick_runner(cands, preferred_regions=["region-a"])
    assert d == ScheduleDecision(site_id="site-l", preferred_miss=True)


def test_no_preference_picks_any_without_miss():
    cands = [r("site-b", ["region-b"], load=2), r("site-a", ["region-a"], load=1)]
    d = pick_runner(cands, preferred_regions=[])
    assert d == ScheduleDecision(site_id="site-a", preferred_miss=False)


def test_least_loaded_among_preferred():
    cands = [r("site-a", ["region-a"], 5), r("site-c", ["region-a"], 2), r("site-b", ["region-a"], 7)]
    d = pick_runner(cands, preferred_regions=["region-a"])
    assert d is not None and d.site_id == "site-c"


def test_tie_breaks_on_site_id_regardless_of_input_order():
    base = [r("site-b", ["region-a"], 1), r("site-a", ["region-a"], 1), r("site-c", ["region-a"], 1)]
    for perm in itertools.permutations(base):
        d = pick_runner(list(perm), preferred_regions=["region-a"])
        assert d is not None and d.site_id == "site-a"


def test_fallback_is_also_least_loaded_then_site_id():
    cands = [r("site-z", ["region-b"], 1), r("site-y", ["region-b"], 1), r("site-x", ["local"], 4)]
    d = pick_runner(cands, preferred_regions=["region-a"])
    assert d == ScheduleDecision(site_id="site-y", preferred_miss=True)


def test_accepts_iterables_and_does_not_mutate_inputs():
    cands = [r("site-b", ["region-a"], 1), r("site-a", ["region-a"], 1)]
    snapshot = list(cands)
    d = pick_runner(iter(cands), preferred_regions=("region-a",))
    assert d is not None and d.site_id == "site-a"
    assert cands == snapshot
