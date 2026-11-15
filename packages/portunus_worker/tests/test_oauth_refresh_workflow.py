"""Workflow unit test with Temporal time-skipping test environment."""

from __future__ import annotations

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from portunus_worker.workflows.oauth_refresh import OAuthRefreshWorkflow

pytestmark = pytest.mark.anyio


@activity.defn(name="list_oauth_refresh_plan")
async def fake_plan(skew_s: int) -> dict:
    return {"due": ["00000000-0000-0000-0000-000000000001"], "next_sleep_s": 60}


@activity.defn(name="refresh_oauth")
async def fake_refresh(server_id: str) -> dict:
    return {"ok": True, "expires_in": 120}


@activity.defn(name="mark_auth_required")
async def fake_mark(server_id: str) -> None:
    return None


async def test_oauth_refresh_workflow_refreshes_due_then_sleeps():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[OAuthRefreshWorkflow],
            activities=[fake_plan, fake_refresh, fake_mark],
        ):
            handle = await env.client.start_workflow(
                OAuthRefreshWorkflow.run,
                id="oauth-refresh-test",
                task_queue="test",
            )
            await env.sleep(1)
            status = await handle.query(OAuthRefreshWorkflow.status)
            assert status.startswith("sleep:") or status.startswith("refreshing:")
            stats = await handle.query(OAuthRefreshWorkflow.stats)
            assert stats["last_due"] == 1
            assert stats["last_failed"] == 0


async def test_oauth_refresh_kick_wakes_early():
    calls: list[str] = []

    @activity.defn(name="list_oauth_refresh_plan")
    async def counting_plan(skew_s: int) -> dict:
        calls.append("plan")
        return {"due": [], "next_sleep_s": 3600}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-kick",
            workflows=[OAuthRefreshWorkflow],
            activities=[counting_plan, fake_refresh, fake_mark],
        ):
            handle = await env.client.start_workflow(
                OAuthRefreshWorkflow.run,
                id="oauth-refresh-kick",
                task_queue="test-kick",
            )
            await env.sleep(1)
            assert len(calls) == 1
            await handle.signal(OAuthRefreshWorkflow.kick)
            await env.sleep(1)
            assert len(calls) >= 2
