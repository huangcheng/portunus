from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import probe as probe_acts
    from portunus_worker.settings import temporal_probe_task_queue


@workflow.defn(name="ConnectEnsureWorkflow")
class ConnectEnsureWorkflow:
    def __init__(self) -> None:
        self._oauth_done = False
        self._status = "init"

    @workflow.signal
    def oauth_complete(self) -> None:
        self._oauth_done = True

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, server_id: str, wait_oauth: bool = False) -> dict:
        if wait_oauth:
            self._status = "await_oauth"
            await workflow.wait_condition(lambda: self._oauth_done)

        probe_q = temporal_probe_task_queue()
        self._status = "probing"
        probe = await workflow.execute_activity(
            probe_acts.probe_server,
            server_id,
            task_queue=probe_q,
            start_to_close_timeout=timedelta(seconds=180),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if probe.get("skipped") and probe.get("reason") == "auth_required":
            self._status = "auth_required"
            return {"ok": False, "probe": probe}

        self._status = "enabling"
        enabled = await workflow.execute_activity(
            probe_acts.enable_server_and_tools,
            server_id,
            task_queue=probe_q,
            start_to_close_timeout=timedelta(seconds=60),
        )
        await workflow.execute_activity(
            probe_acts.ensure_full_grants,
            start_to_close_timeout=timedelta(seconds=60),
        )

        self._status = "starting_children"
        await workflow.execute_activity(
            probe_acts.start_connect_children,
            server_id,
            start_to_close_timeout=timedelta(seconds=30),
        )
        self._status = "done"
        return {"ok": True, "probe": probe, "enabled": enabled}
