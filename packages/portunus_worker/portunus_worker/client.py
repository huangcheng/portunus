"""Gateway-facing Temporal client helpers (soft-fail when TEMPORAL_HOST unset)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .settings import temporal_enabled, temporal_host, temporal_namespace, temporal_task_queue

log = logging.getLogger("portunus.worker.client")


async def _client():
    if not temporal_enabled():
        return None
    from temporalio.client import Client

    return await Client.connect(temporal_host(), namespace=temporal_namespace())


def oauth_refresh_id(server_id: uuid.UUID) -> str:
    return f"oauth-refresh:{server_id}"


def server_probe_id(server_id: uuid.UUID) -> str:
    return f"server-probe:{server_id}"


def connect_ensure_id(server_id: uuid.UUID) -> str:
    return f"connect-ensure:{server_id}"


async def signal_oauth_tokens_stored(
    server_id: uuid.UUID,
    *,
    expires_in: int | None,
    has_refresh: bool,
) -> None:
    """Start or signal the OAuth refresh workflow after a successful code exchange."""
    client = await _client()
    if client is None:
        return
    from .workflows.oauth_refresh import OAuthRefreshWorkflow, TokensStored

    wid = oauth_refresh_id(server_id)
    handle = client.get_workflow_handle(wid)
    try:
        await handle.signal(
            OAuthRefreshWorkflow.tokens_stored,
            TokensStored(expires_in=expires_in, has_refresh=has_refresh),
        )
        return
    except Exception:  # noqa: BLE001 — workflow may not exist yet
        log.debug("oauth refresh signal miss; starting workflow", exc_info=True)
    await client.start_workflow(
        OAuthRefreshWorkflow.run,
        args=[str(server_id), expires_in, has_refresh],
        id=wid,
        task_queue=temporal_task_queue(),
    )


async def start_server_probe(server_id: uuid.UUID) -> None:
    client = await _client()
    if client is None:
        return
    from .workflows.server_probe import ServerProbeWorkflow

    wid = server_probe_id(server_id)
    try:
        await client.start_workflow(
            ServerProbeWorkflow.run,
            args=[str(server_id)],
            id=wid,
            task_queue=temporal_task_queue(),
        )
    except Exception:  # noqa: BLE001 — already running is fine
        log.debug("server probe start skipped", exc_info=True)


async def start_connect_ensure(server_id: uuid.UUID, *, wait_oauth: bool = False) -> str | None:
    client = await _client()
    if client is None:
        return None
    from .workflows.connect_ensure import ConnectEnsureWorkflow

    wid = connect_ensure_id(server_id)
    try:
        await client.start_workflow(
            ConnectEnsureWorkflow.run,
            args=[str(server_id), wait_oauth],
            id=wid,
            task_queue=temporal_task_queue(),
        )
    except Exception:  # noqa: BLE001
        log.debug("connect-ensure already running or failed to start", exc_info=True)
    return wid


async def start_fleet_skills_sync(*, hosts: list[str] | None = None) -> str | None:
    client = await _client()
    if client is None:
        return None
    from .workflows.fleet_skills import FleetSkillsSyncWorkflow

    wid = f"fleet-skills-sync:{uuid.uuid4()}"
    await client.start_workflow(
        FleetSkillsSyncWorkflow.run,
        args=[hosts or []],
        id=wid,
        task_queue=temporal_task_queue(),
    )
    return wid


async def ensure_fleet_skills_schedule() -> None:
    """Create a daily Schedule for fleet skill sync (idempotent create)."""
    client = await _client()
    if client is None:
        return
    from datetime import timedelta

    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleSpec,
    )

    from .workflows.fleet_skills import FleetSkillsSyncWorkflow

    schedule_id = "fleet-skills-sync-daily"
    action = ScheduleActionStartWorkflow(
        FleetSkillsSyncWorkflow.run,
        args=[[]],
        id="fleet-skills-sync-scheduled",
        task_queue=temporal_task_queue(),
    )
    spec = ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(days=1))])
    try:
        await client.create_schedule(schedule_id, Schedule(action=action, spec=spec))
        log.info("created Temporal schedule %s", schedule_id)
    except Exception:  # noqa: BLE001 — already exists
        log.debug("fleet schedule create skipped", exc_info=True)


async def temporal_status() -> dict[str, Any]:
    if not temporal_enabled():
        return {"enabled": False}
    try:
        client = await _client()
        assert client is not None
        return {
            "enabled": True,
            "host": temporal_host(),
            "namespace": temporal_namespace(),
            "task_queue": temporal_task_queue(),
        }
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "error": str(e)}


def _dt(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat().replace("+00:00", "Z")
    except Exception:  # noqa: BLE001
        return str(value)


def _workflow_summary(exec: Any) -> dict[str, Any]:
    """Normalize a WorkflowExecution / WorkflowExecutionDescription for the dashboard."""
    status = getattr(exec, "status", None)
    status_name = getattr(status, "name", None) or (str(status) if status is not None else None)
    return {
        "workflow_id": getattr(exec, "id", None) or getattr(exec, "workflow_id", None),
        "run_id": getattr(exec, "run_id", None),
        "workflow_type": getattr(getattr(exec, "workflow_type", None), "name", None)
        or getattr(exec, "workflow_type", None),
        "task_queue": getattr(exec, "task_queue", None),
        "status": status_name,
        "start_time": _dt(getattr(exec, "start_time", None)),
        "close_time": _dt(getattr(exec, "close_time", None)),
        "execution_time": _dt(getattr(exec, "execution_time", None)),
        "history_length": getattr(exec, "history_length", None),
    }


async def list_workflows(
    *,
    query: str = "",
    page_size: int = 50,
) -> dict[str, Any]:
    """List recent workflows on the Portunus task queue (and optional Visibility query)."""
    client = await _client()
    if client is None:
        return {"enabled": False, "items": [], "next_page_token": None}
    q = (query or "").strip()
    # Default: workflows on our control queue so the console stays focused.
    if not q:
        q = f'TaskQueue = "{temporal_task_queue()}"'
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    try:
        pager = client.list_workflows(query=q, page_size=min(max(page_size, 1), 100))
        async for exec in pager:
            items.append(_workflow_summary(exec))
            if len(items) >= page_size:
                break
        # temporalio AsyncWorkflowPager may expose next_page_token after iteration
        raw = getattr(pager, "next_page_token", None) or getattr(pager, "_next_page_token", None)
        if isinstance(raw, (bytes, bytearray)) and raw:
            import base64

            next_token = base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
        elif isinstance(raw, str) and raw:
            next_token = raw
    except Exception as e:  # noqa: BLE001
        return {
            "enabled": True,
            "items": items,
            "next_page_token": None,
            "error": str(e),
            "query": q,
        }
    return {"enabled": True, "items": items, "next_page_token": next_token, "query": q}


async def describe_workflow(
    workflow_id: str,
    *,
    run_id: str | None = None,
    history_limit: int = 80,
) -> dict[str, Any]:
    """Describe one workflow and return a truncated event history for the drawer."""
    client = await _client()
    if client is None:
        raise RuntimeError("TEMPORAL_HOST is not configured")
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    summary = _workflow_summary(desc)
    # Query status when the workflow exposes it (OAuth / probe / connect).
    query_status: str | None = None
    try:
        query_status = await handle.query("status")
        if query_status is not None:
            query_status = str(query_status)
    except Exception:  # noqa: BLE001 — workflow may not define status
        query_status = None

    events: list[dict[str, Any]] = []
    try:
        count = 0
        async for event in handle.fetch_history_events():
            et = getattr(event, "event_type", None)
            events.append(
                {
                    "event_id": getattr(event, "event_id", None),
                    "event_type": getattr(et, "name", None) or str(et),
                    "event_time": _dt(getattr(event, "event_time", None)),
                }
            )
            count += 1
            if count >= history_limit:
                break
    except Exception as e:  # noqa: BLE001
        return {
            "enabled": True,
            "workflow": summary,
            "query_status": query_status,
            "events": events,
            "error": str(e),
        }

    # Surface failure message from describe when closed as failed.
    failure: str | None = None
    try:
        raw_fail = getattr(desc, "raw_description", None)
        # Prefer close failure attributes when available on description
        close_status = summary.get("status")
        if close_status and "FAILED" in str(close_status).upper():
            failure = "Workflow failed — see history events"
    except Exception:  # noqa: BLE001
        failure = None

    return {
        "enabled": True,
        "workflow": summary,
        "query_status": query_status,
        "events": events,
        "failure": failure,
    }
