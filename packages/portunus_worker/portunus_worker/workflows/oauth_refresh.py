"""Shared OAuth refresh workflow — one durable loop for every remote with a refresh token.

Temporal keeps this alive across restarts. Per-server failures stay in activities
(retries + catch), so one bad provider cannot stall the queue.

Includes paused Apps (``enabled=false``): access tokens keep renewing while the
App is hidden from agents, so Resume does not require a fresh OAuth dance.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import oauth as oauth_acts
    from portunus_worker.settings import oauth_refresh_skew_s

# Bound history; continue-as-new after this many scan cycles.
_CONTINUE_EVERY = 200
_WORKFLOW_ID = "oauth-refresh"


def shared_workflow_id() -> str:
    return _WORKFLOW_ID


@workflow.defn(name="OAuthRefreshWorkflow")
class OAuthRefreshWorkflow:
    def __init__(self) -> None:
        self._kick = False
        self._status = "init"
        self._last_due = 0
        self._last_failed = 0

    @workflow.signal
    def kick(self) -> None:
        """Wake early after tokens are stored (or operator nudge)."""
        self._kick = True

    # Back-compat: old per-server workflows signaled tokens_stored; ignore payload.
    @workflow.signal
    def tokens_stored(self, payload: object = None) -> None:
        self._kick = True

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.query
    def stats(self) -> dict:
        return {"last_due": self._last_due, "last_failed": self._last_failed}

    @workflow.run
    async def run(self) -> None:
        skew = oauth_refresh_skew_s()
        cycles = 0
        while True:
            self._kick = False
            self._status = "scanning"
            plan = await workflow.execute_activity(
                oauth_acts.list_oauth_refresh_plan,
                skew,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            due = list(plan.get("due") or [])
            self._last_due = len(due)
            failed = 0
            for server_id in due:
                self._status = f"refreshing:{server_id}"
                try:
                    result = await workflow.execute_activity(
                        oauth_acts.refresh_oauth,
                        server_id,
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                except ActivityError:
                    # Transient activity failure (timeout/network) — do NOT clear
                    # has_oauth_refresh; the next wave will retry. Only ok=False
                    # below means the registry classified a real auth failure.
                    failed += 1
                    continue
                if not result.get("ok"):
                    failed += 1
                    await workflow.execute_activity(
                        oauth_acts.mark_auth_required,
                        server_id,
                        start_to_close_timeout=timedelta(seconds=30),
                    )
            self._last_failed = failed

            sleep_s = int(plan.get("next_sleep_s") or 300)
            sleep_s = max(30, min(sleep_s, 3600))
            self._status = f"sleep:{sleep_s} due={len(due)} fail={failed}"
            try:
                await workflow.wait_condition(
                    lambda: self._kick,
                    timeout=timedelta(seconds=sleep_s),
                )
            except TimeoutError:
                pass

            cycles += 1
            if cycles >= _CONTINUE_EVERY:
                workflow.continue_as_new()
