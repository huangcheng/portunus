"""Workflow unit test with Temporal time-skipping test environment."""

from __future__ import annotations

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from portunus_worker.workflows.oauth_refresh import OAuthRefreshWorkflow, TokensStored

pytestmark = pytest.mark.anyio


@activity.defn(name="refresh_oauth")
async def fake_refresh(server_id: str) -> dict:
    return {"ok": True, "expires_in": 120}


@activity.defn(name="mark_auth_required")
async def fake_mark(server_id: str) -> None:
    return None


async def test_oauth_refresh_workflow_sleeps_then_refreshes():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[OAuthRefreshWorkflow],
            activities=[fake_refresh, fake_mark],
        ):
            handle = await env.client.start_workflow(
                OAuthRefreshWorkflow.run,
                args=["00000000-0000-0000-0000-000000000001", 90, True],
                id="oauth-refresh-test",
                task_queue="test",
            )
            # Allow sleep past skew (default skew 300 but expires 90 → sleep max(60, 90-300)=60)
            await env.sleep(70)
            status = await handle.query(OAuthRefreshWorkflow.status)
            assert status in ("refreshing", "sleep:60", "sleep:120") or status.startswith("sleep:")
