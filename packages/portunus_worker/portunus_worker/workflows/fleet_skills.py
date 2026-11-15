from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import fleet as fleet_acts
    from portunus_worker.settings import fleet_hosts


@workflow.defn(name="FleetSkillsSyncWorkflow")
class FleetSkillsSyncWorkflow:
    @workflow.run
    async def run(self, hosts: list[str] | None = None) -> dict:
        targets = [h for h in (hosts or []) if h] or fleet_hosts()
        if not targets:
            return {"ok": False, "error": "no hosts (set PORTUNUS_FLEET_HOSTS or pass hosts)"}
        results = []
        for host in targets:
            result = await workflow.execute_activity(
                fleet_acts.sync_skills_host,
                host,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            results.append(result)
        ok = all(r.get("ok") for r in results)
        return {"ok": ok, "results": results}
