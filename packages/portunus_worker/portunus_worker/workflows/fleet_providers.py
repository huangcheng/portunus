from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from portunus_worker.activities import fleet as fleet_acts
    from portunus_worker.settings import fleet_hosts

DEFAULT_COMPONENTS = ("rclone", "skills", "skill_bridge")


def normalize_components(raw: list[str] | None) -> list[str]:
    """Deduplicate and validate component names; default = all three."""
    if not raw:
        return list(DEFAULT_COMPONENTS)
    aliases = {
        "rclone": "rclone",
        "skills": "skills",
        "skill": "skills",
        "trees": "skills",
        "cli": "skills",
        "clis": "skills",
        "skill_bridge": "skill_bridge",
        "skill-bridge": "skill_bridge",
        "bridge": "skill_bridge",
    }
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        key = aliases.get(item.strip().casefold())
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out or list(DEFAULT_COMPONENTS)


@workflow.defn(name="FleetProvidersUpdateWorkflow")
class FleetProvidersUpdateWorkflow:
    """Refresh rclone, skill trees/CLIs, and skill_bridge on fleet hosts."""

    def __init__(self) -> None:
        self._status = "init"
        self._host: str | None = None
        self._done = 0
        self._failed = 0
        self._total = 0

    @workflow.query
    def status(self) -> str:
        if self._status == "host" and self._host:
            return f"host:{self._host}"
        return self._status

    @workflow.query
    def stats(self) -> dict:
        return {
            "done": self._done,
            "failed": self._failed,
            "total": self._total,
            "host": self._host,
        }

    @workflow.run
    async def run(
        self,
        hosts: list[str] | None = None,
        components: list[str] | None = None,
    ) -> dict:
        targets = [h for h in (hosts or []) if h] or fleet_hosts()
        comps = normalize_components(components)
        if not targets:
            self._status = "no_hosts"
            return {
                "ok": False,
                "error": "no hosts (set PORTUNUS_FLEET_HOSTS or pass hosts)",
                "components": comps,
            }
        self._total = len(targets)
        results = []
        for host in targets:
            self._status = "host"
            self._host = host
            result = await workflow.execute_activity(
                fleet_acts.update_providers_host,
                args=[host, comps],
                start_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            results.append(result)
            if result.get("ok"):
                self._done += 1
            else:
                self._failed += 1
        self._host = None
        ok = all(r.get("ok") for r in results)
        self._status = "done" if ok else "failed"
        return {"ok": ok, "components": comps, "results": results}
