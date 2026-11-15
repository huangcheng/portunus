from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import probe as probe_acts
    from portunus_worker.settings import probe_interval_s


@workflow.defn(name="ServerProbeWorkflow")
class ServerProbeWorkflow:
    def __init__(self) -> None:
        self._paused = False
        self._status = "init"

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, server_id: str) -> None:
        interval = probe_interval_s()
        while True:
            if self._paused:
                self._status = "paused"
                await workflow.wait_condition(lambda: not self._paused)
            self._status = "probing"
            await workflow.execute_activity(
                probe_acts.probe_server,
                server_id,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            self._status = f"sleep:{interval}"
            await workflow.sleep(timedelta(seconds=interval))
