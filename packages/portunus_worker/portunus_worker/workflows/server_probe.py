from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import probe as probe_acts
    from portunus_worker.settings import probe_interval_s, temporal_probe_task_queue


@workflow.defn(name="ServerProbeWorkflow")
class ServerProbeWorkflow:
    def __init__(self) -> None:
        self._paused = False
        self._force = False
        self._status = "init"

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def probe_now(self) -> None:
        """Wake from interval sleep and probe immediately (post-credential / operator)."""
        self._force = True

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, server_id: str) -> None:
        interval = probe_interval_s()
        # Spread the first wave so N workflows do not stampede the probe pool.
        # Deterministic (no hash()): Temporal replays require a stable stagger.
        stagger = sum(ord(c) for c in server_id) % min(60, max(1, interval // 4))
        if stagger:
            self._status = f"stagger:{stagger}"
            try:
                await workflow.wait_condition(
                    lambda: self._force or self._paused,
                    timeout=timedelta(seconds=stagger),
                )
            except TimeoutError:
                pass
        while True:
            if self._paused:
                self._status = "paused"
                await workflow.wait_condition(lambda: not self._paused or self._force)

            self._force = False
            self._status = "probing"
            # Dedicated queue: only the gateway-embedded worker polls it (sole-client pool).
            # Probe failures must not kill this long-running workflow — sleep and retry.
            try:
                await workflow.execute_activity(
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
                self._status = "probe_failed"

            self._status = f"sleep:{interval}"
            try:
                await workflow.wait_condition(
                    lambda: self._force or self._paused,
                    timeout=timedelta(seconds=interval),
                )
            except TimeoutError:
                pass
