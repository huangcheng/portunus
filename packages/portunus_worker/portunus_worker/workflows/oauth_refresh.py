from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import oauth as oauth_acts
    from portunus_worker.settings import oauth_refresh_skew_s


@dataclass
class TokensStored:
    expires_in: int | None
    has_refresh: bool


@workflow.defn(name="OAuthRefreshWorkflow")
class OAuthRefreshWorkflow:
    def __init__(self) -> None:
        self._wake: TokensStored | None = None
        self._status = "init"

    @workflow.signal
    def tokens_stored(self, payload: TokensStored) -> None:
        self._wake = payload

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(
        self, server_id: str, expires_in: int | None = None, has_refresh: bool = False
    ) -> None:
        skew = oauth_refresh_skew_s()
        current_expires = expires_in
        current_has_refresh = has_refresh

        while True:
            if self._wake is not None:
                wake = self._wake
                self._wake = None
                current_expires = wake.expires_in
                current_has_refresh = wake.has_refresh

            if not current_has_refresh:
                self._status = "no_refresh_token"
                await workflow.wait_condition(lambda: self._wake is not None)
                continue

            sleep_s = max(60, (current_expires or 3600) - skew)
            self._status = f"sleep:{sleep_s}"
            try:
                await workflow.wait_condition(
                    lambda: self._wake is not None,
                    timeout=timedelta(seconds=sleep_s),
                )
                continue
            except TimeoutError:
                pass

            self._status = "refreshing"
            result = await workflow.execute_activity(
                oauth_acts.refresh_oauth,
                server_id,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            if not result.get("ok"):
                self._status = "auth_required"
                await workflow.execute_activity(
                    oauth_acts.mark_auth_required,
                    server_id,
                    start_to_close_timeout=timedelta(seconds=30),
                )
                await workflow.wait_condition(lambda: self._wake is not None)
                continue

            current_expires = int(result.get("expires_in") or current_expires or 3600)
            current_has_refresh = True
