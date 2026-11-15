"""Shared ServerProbeWorkflow unit tests (Temporal time-skipping)."""

from __future__ import annotations

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from portunus_worker.workflows.server_probe import ServerProbeWorkflow

pytestmark = pytest.mark.anyio

# Activities use temporal_probe_task_queue(); point it at the test worker queue.
_PROBE_Q = "test-probe-shared"


@pytest.fixture(autouse=True)
def _probe_queue_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_PROBE_TASK_QUEUE", _PROBE_Q)


@activity.defn(name="list_probe_plan")
async def fake_plan(interval_s: int) -> dict:
    return {"due": ["00000000-0000-0000-0000-000000000001"], "next_sleep_s": 60}


@activity.defn(name="probe_server")
async def fake_probe(server_id: str) -> dict:
    return {"skipped": False, "ok": True, "tool_count": 1}


async def test_shared_probe_workflow_probes_due_then_sleeps():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=_PROBE_Q,
            workflows=[ServerProbeWorkflow],
            activities=[fake_plan, fake_probe],
        ):
            handle = await env.client.start_workflow(
                ServerProbeWorkflow.run,
                id="server-probe-test",
                task_queue=_PROBE_Q,
            )
            await env.sleep(1)
            status = await handle.query(ServerProbeWorkflow.status)
            assert status.startswith("sleep:") or status.startswith("probing:")
            stats = await handle.query(ServerProbeWorkflow.stats)
            assert stats["last_due"] == 1
            assert stats["last_failed"] == 0


async def test_probe_now_kick_wakes_and_probes_priority():
    plan_calls: list[int] = []
    probe_calls: list[str] = []
    target = "00000000-0000-0000-0000-000000000099"

    @activity.defn(name="list_probe_plan")
    async def counting_plan(interval_s: int) -> dict:
        plan_calls.append(1)
        return {"due": [], "next_sleep_s": 3600}

    @activity.defn(name="probe_server")
    async def counting_probe(server_id: str) -> dict:
        probe_calls.append(server_id)
        return {"skipped": False, "ok": True, "tool_count": 0}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=_PROBE_Q,
            workflows=[ServerProbeWorkflow],
            activities=[counting_plan, counting_probe],
        ):
            handle = await env.client.start_workflow(
                ServerProbeWorkflow.run,
                id="server-probe-kick",
                task_queue=_PROBE_Q,
            )
            await env.sleep(1)
            assert len(plan_calls) == 1
            assert probe_calls == []
            await handle.signal(ServerProbeWorkflow.probe_now, args=[target])
            await env.sleep(1)
            assert len(plan_calls) >= 2, plan_calls
            assert target in probe_calls
