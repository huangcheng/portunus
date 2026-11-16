"""Shared fleet providers update — durable loop for rclone / skills / skill_bridge.

One always-on workflow (like server-probe / oauth-refresh). Daily by default;
operator **Update providers** signals ``kick``.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import fleet as fleet_acts
    from portunus_worker.settings import fleet_hosts, fleet_providers_interval_s

_CONTINUE_EVERY = 50
_WORKFLOW_ID = "fleet-providers-update"

DEFAULT_COMPONENTS = ("rclone", "skills", "skill_bridge")


def shared_workflow_id() -> str:
    return _WORKFLOW_ID


def normalize_components(raw: list[str] | None) -> list[str]:
    """Deduplicate and validate component names; default = all three."""
    if not raw:
        return list(DEFAULT_COMPONENTS)
    aliases = {
        "rclone": "rclone",
        "skills": "skills",
        "skill": "skills",
        "trees": "skills",
        "cli": "skills",
        "clis": "skills",
        "skill_bridge": "skill_bridge",
        "skill-bridge": "skill_bridge",
        "bridge": "skill_bridge",
    }
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        key = aliases.get(item.strip().casefold())
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out or list(DEFAULT_COMPONENTS)


@workflow.defn(name="FleetProvidersUpdateWorkflow")
class FleetProvidersUpdateWorkflow:
    """Refresh rclone, skill trees/CLIs, and skill_bridge on fleet hosts."""

    def __init__(self) -> None:
        self._kick = False
        self._paused = False
        self._status = "init"
        self._host: str | None = None
        self._done = 0
        self._failed = 0
        self._total = 0
        self._components_override: list[str] | None = None
        self._hosts_override: list[str] | None = None

    @workflow.signal
    def kick(
        self,
        hosts: list[str] | None = None,
        components: list[str] | None = None,
    ) -> None:
        """Wake early (operator Update providers / boot nudge)."""
        self._kick = True
        if hosts:
            self._hosts_override = [h for h in hosts if isinstance(h, str) and h.strip()]
        if components is not None:
            self._components_override = list(components)

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False
        self._kick = True

    @workflow.query
    def status(self) -> str:
        if self._paused:
            return "paused"
        if self._status == "host" and self._host:
            return f"host:{self._host}"
        return self._status

    @workflow.query
    def stats(self) -> dict:
        return {
            "done": self._done,
            "failed": self._failed,
            "total": self._total,
            "host": self._host,
            "paused": self._paused,
        }

    async def _run_wave(self) -> None:
        hosts_ov = self._hosts_override
        comps_ov = self._components_override
        self._hosts_override = None
        self._components_override = None

        targets = [h for h in (hosts_ov or []) if h] or fleet_hosts()
        comps = normalize_components(comps_ov)
        if not targets:
            self._status = "no_hosts"
            self._done = 0
            self._failed = 0
            self._total = 0
            return

        self._total = len(targets)
        self._done = 0
        self._failed = 0
        for host in targets:
            self._status = "host"
            self._host = host
            try:
                result = await workflow.execute_activity(
                    fleet_acts.update_providers_host,
                    args=[host, comps],
                    start_to_close_timeout=timedelta(minutes=45),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            except ActivityError:
                self._failed += 1
                continue
            if result.get("ok"):
                self._done += 1
            else:
                self._failed += 1
        self._host = None
        self._status = "done" if self._failed == 0 else "failed"

    @workflow.run
    async def run(
        self,
        hosts: list[str] | None = None,
        components: list[str] | None = None,
    ) -> None:
        # Legacy one-shot / schedule start args: only non-empty lists force a first wave.
        if hosts:
            self._hosts_override = [h for h in hosts if isinstance(h, str) and h.strip()]
        if components:
            self._components_override = list(components)

        interval = fleet_providers_interval_s()
        cycles = 0
        # Only an explicit start/kick override forces an immediate wave; otherwise
        # sleep until kick / interval (ensure + kick from the API covers first sync).
        pending_wave = bool(self._hosts_override or self._components_override)
        while True:
            if self._paused:
                self._status = "paused"
                await workflow.wait_condition(lambda: (not self._paused) or self._kick)

            kicked = self._kick
            self._kick = False
            if pending_wave or kicked or cycles > 0:
                if not self._paused:
                    await self._run_wave()
            pending_wave = False

            sleep_s = max(60, interval)
            if not self._paused and self._status != "no_hosts":
                # humanize_query_status parses due=/fail= after sleep:
                self._status = f"sleep:{sleep_s} due={self._done} fail={self._failed}"
            try:
                await workflow.wait_condition(
                    lambda: self._kick or self._paused,
                    timeout=timedelta(seconds=sleep_s),
                )
            except TimeoutError:
                pass

            cycles += 1
            if cycles >= _CONTINUE_EVERY:
                workflow.continue_as_new()
