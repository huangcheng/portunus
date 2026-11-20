"""Shared server-probe workflow — one durable loop for every enabled server.

Probe activities run on ``portunus-probe`` (gateway-embedded sole-client pool).
Per-server failures stay in activities so one hung probe cannot stall the queue.
Site pick / GFW failover lives in ``Registry.probe`` (preferred_regions + SiteHealth).

**Credential health:** this workflow *is* the fleet credential check. First-party
builtins (Graph / JMAP) and remote MCP ``tools/list`` must authenticate when a
vault Authorization is present so revoked tokens / bad passwords become
``auth_required`` here — do not add a parallel Temporal “credential-health”
loop. OAuth access-token renewal stays in ``OAuthRefreshWorkflow``.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import probe as probe_acts
    from portunus_worker.settings import probe_interval_s, temporal_probe_task_queue

_CONTINUE_EVERY = 200
_WORKFLOW_ID = "server-probe"


def shared_workflow_id() -> str:
    return _WORKFLOW_ID


@workflow.defn(name="ServerProbeWorkflow")
class ServerProbeWorkflow:
    def __init__(self) -> None:
        self._kick = False
        self._paused = False
        self._priority: list[str] = []
        self._status = "init"
        self._last_due = 0
        self._last_failed = 0

    @workflow.signal
    def kick(self) -> None:
        """Wake early (operator / boot nudge)."""
        self._kick = True

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False
        self._kick = True

    @workflow.signal
    def probe_now(self, server_id: str = "") -> None:
        """Wake; when ``server_id`` is set, probe that server first on the next wave."""
        self._kick = True
        sid = server_id.strip() if isinstance(server_id, str) else ""
        if sid and sid not in self._priority:
            self._priority.append(sid)

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.query
    def stats(self) -> dict:
        return {
            "last_due": self._last_due,
            "last_failed": self._last_failed,
            "paused": self._paused,
            "priority": list(self._priority),
        }

    @workflow.run
    async def run(self, _legacy_server_id: str | None = None) -> None:
        # Accept unused legacy arg so old per-server start_workflow shapes still type-check
        # if a stale handle somehow replays; new starts pass no args.
        del _legacy_server_id
        interval = probe_interval_s()
        cycles = 0
        while True:
            if self._paused:
                self._status = "paused"
                await workflow.wait_condition(lambda: not self._paused or self._kick)

            self._kick = False
            priority = list(self._priority)
            self._priority.clear()

            self._status = "scanning"
            plan = await workflow.execute_activity(
                probe_acts.list_probe_plan,
                interval,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            due_ids = list(plan.get("due") or [])
            # Priority first (probe_now), then the rest of the wave without dupes.
            seen: set[str] = set()
            due: list[str] = []
            for sid in priority + due_ids:
                if sid in seen:
                    continue
                seen.add(sid)
                due.append(sid)

            self._last_due = len(due)
            failed = 0
            for server_id in due:
                if self._paused:
                    break
                self._status = f"probing:{server_id}"
                try:
                    result = await workflow.execute_activity(
                        probe_acts.probe_server,
                        server_id,
                        task_queue=temporal_probe_task_queue(),
                        start_to_close_timeout=timedelta(seconds=120),
                        retry_policy=RetryPolicy(
                            maximum_attempts=3,
                            initial_interval=timedelta(seconds=5),
                            backoff_coefficient=2.0,
                        ),
                    )
                except ActivityError:
                    failed += 1
                    continue
                if result.get("ok") is False and not result.get("skipped"):
                    failed += 1

            self._last_failed = failed
            sleep_s = int(plan.get("next_sleep_s") or interval)
            sleep_s = max(30, min(sleep_s, 3600))
            self._status = f"sleep:{sleep_s} due={len(due)} fail={failed}"
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
