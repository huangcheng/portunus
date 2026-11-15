"""Gateway-facing Temporal client helpers (soft-fail when TEMPORAL_HOST unset)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .settings import temporal_enabled, temporal_host, temporal_namespace, temporal_task_queue

log = logging.getLogger("portunus.worker.client")


def _event_type_label(et: Any) -> str:
    """Human label for a Temporal history event_type (protobuf int or enum)."""
    raw: str | None = None
    if et is None:
        return "Unknown"
    name = getattr(et, "name", None)
    if isinstance(name, str) and name:
        raw = name
    else:
        try:
            from temporalio.api.enums.v1 import EventType

            raw = EventType.Name(int(et))
        except Exception:  # noqa: BLE001 — fall back to string form
            raw = str(et)
    if raw.startswith("EVENT_TYPE_"):
        raw = raw[len("EVENT_TYPE_") :]
    # WORKFLOW_EXECUTION_STARTED → WorkflowExecutionStarted
    if "_" in raw and raw.upper() == raw:
        return "".join(part.title() for part in raw.split("_"))
    return raw


def _event_summary(event: Any, label: str) -> str | None:
    """Short secondary line: signal name, activity type, timer id, etc."""
    lower = label.lower()
    try:
        if "signaled" in lower:
            attrs = getattr(event, "workflow_execution_signaled_event_attributes", None)
            name = getattr(attrs, "signal_name", None) if attrs else None
            return f"signal {name}" if name else None
        if "activitytaskscheduled" in lower.replace("_", ""):
            attrs = getattr(event, "activity_task_scheduled_event_attributes", None)
            at = getattr(attrs, "activity_type", None) if attrs else None
            name = getattr(at, "name", None) if at else None
            return name
        if "timerstarted" in lower.replace("_", ""):
            attrs = getattr(event, "timer_started_event_attributes", None)
            tid = getattr(attrs, "timer_id", None) if attrs else None
            return f"timer {tid}" if tid else None
        if "markerrecorded" in lower.replace("_", ""):
            attrs = getattr(event, "marker_recorded_event_attributes", None)
            name = getattr(attrs, "marker_name", None) if attrs else None
            return name
    except Exception:  # noqa: BLE001
        return None
    return None


async def _client():
    if not temporal_enabled():
        return None
    from temporalio.client import Client

    return await Client.connect(temporal_host(), namespace=temporal_namespace())


def oauth_refresh_id() -> str:
    """Fixed workflow id for the shared OAuth refresher."""
    from .workflows.oauth_refresh import shared_workflow_id

    return shared_workflow_id()


def legacy_oauth_refresh_id(server_id: uuid.UUID) -> str:
    """Pre-shared per-server workflow id (terminate on boot)."""
    return f"oauth-refresh:{server_id}"


def server_probe_id() -> str:
    """Fixed workflow id for the shared server probe scheduler."""
    from .workflows.server_probe import shared_workflow_id

    return shared_workflow_id()


def legacy_server_probe_id(server_id: uuid.UUID) -> str:
    """Pre-shared per-server workflow id (terminate on boot)."""
    return f"server-probe:{server_id}"


def connect_ensure_id(server_id: uuid.UUID) -> str:
    return f"connect-ensure:{server_id}"


async def ensure_oauth_refresh_workflow() -> None:
    """Start the shared OAuth refresher and retire per-server legacy workflows."""
    client = await _client()
    if client is None:
        return
    from .workflows.oauth_refresh import OAuthRefreshWorkflow

    wid = oauth_refresh_id()
    try:
        await client.start_workflow(
            OAuthRefreshWorkflow.run,
            id=wid,
            task_queue=temporal_task_queue(),
        )
        log.info("started shared OAuthRefreshWorkflow id=%s", wid)
    except Exception:  # noqa: BLE001 — already running is fine
        log.debug("shared oauth refresh already running", exc_info=True)

    # Retire old per-server oauth-refresh:{uuid} executions so they stop racing.
    try:
        async for wf in client.list_workflows(
            query='WorkflowType = "OAuthRefreshWorkflow" AND ExecutionStatus = "Running"'
        ):
            if wf.id == wid:
                continue
            if not wf.id.startswith("oauth-refresh:"):
                continue
            try:
                await client.get_workflow_handle(wf.id).terminate(
                    reason="replaced by shared oauth-refresh workflow"
                )
                log.info("terminated legacy OAuthRefreshWorkflow id=%s", wf.id)
            except Exception:  # noqa: BLE001
                log.debug("legacy oauth terminate skipped id=%s", wf.id, exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("legacy oauth refresh sweep skipped", exc_info=True)


async def signal_oauth_tokens_stored(
    server_id: uuid.UUID,
    *,
    expires_in: int | None,
    has_refresh: bool,
) -> None:
    """Kick the shared OAuth refresher after a successful code exchange.

    ``server_id`` / ``expires_in`` / ``has_refresh`` are kept for call-site
    compatibility; the shared workflow re-reads due servers from the DB.
    """
    del expires_in  # plan activity reads token_expires_at from DB
    if not has_refresh:
        return
    client = await _client()
    if client is None:
        return
    from .workflows.oauth_refresh import OAuthRefreshWorkflow

    wid = oauth_refresh_id()
    handle = client.get_workflow_handle(wid)
    try:
        await handle.signal(OAuthRefreshWorkflow.kick)
        log.debug("oauth refresh kick server=%s", server_id)
        return
    except Exception:  # noqa: BLE001 — workflow may not exist yet
        log.debug("oauth refresh kick miss; ensuring workflow", exc_info=True)
    await ensure_oauth_refresh_workflow()
    try:
        await client.get_workflow_handle(wid).signal(OAuthRefreshWorkflow.kick)
    except Exception:  # noqa: BLE001
        log.debug("oauth refresh kick after ensure failed", exc_info=True)


async def ensure_server_probe_workflow() -> None:
    """Start the shared server-probe scheduler and retire per-server legacy workflows."""
    client = await _client()
    if client is None:
        return
    from .workflows.server_probe import ServerProbeWorkflow

    wid = server_probe_id()
    try:
        await client.start_workflow(
            ServerProbeWorkflow.run,
            id=wid,
            task_queue=temporal_task_queue(),
        )
        log.info("started shared ServerProbeWorkflow id=%s", wid)
    except Exception:  # noqa: BLE001 — already running is fine
        log.debug("shared server probe already running", exc_info=True)

    try:
        async for wf in client.list_workflows(
            query='WorkflowType = "ServerProbeWorkflow" AND ExecutionStatus = "Running"'
        ):
            if wf.id == wid:
                continue
            if not wf.id.startswith("server-probe:"):
                continue
            try:
                await client.get_workflow_handle(wf.id).terminate(
                    reason="replaced by shared server-probe workflow"
                )
                log.info("terminated legacy ServerProbeWorkflow id=%s", wf.id)
            except Exception:  # noqa: BLE001
                log.debug("legacy probe terminate skipped id=%s", wf.id, exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("legacy server probe sweep skipped", exc_info=True)


async def start_server_probe(server_id: uuid.UUID) -> None:
    """Ensure shared probe workflow; prioritize this server on the next wave."""
    await request_server_probe(server_id, immediate=True)


async def request_server_probe(server_id: uuid.UUID, *, immediate: bool = True) -> None:
    """Ensure shared ServerProbeWorkflow; optionally probe_now(server_id)."""
    client = await _client()
    if client is None:
        return
    from .workflows.server_probe import ServerProbeWorkflow

    wid = server_probe_id()
    await ensure_server_probe_workflow()
    if not immediate:
        return
    try:
        await client.get_workflow_handle(wid).signal(
            ServerProbeWorkflow.probe_now, args=[str(server_id)]
        )
    except Exception:  # noqa: BLE001
        log.debug("server probe_now signal skipped server=%s", server_id, exc_info=True)


async def ensure_enabled_server_probes() -> int:
    """Boot helper: ensure shared probe workflow (legacy name kept for call sites)."""
    await ensure_server_probe_workflow()
    log.info("ensured shared ServerProbeWorkflow id=%s", server_probe_id())
    return 1


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


def _encode_page_token(raw: Any) -> str | None:
    if isinstance(raw, (bytes, bytearray)) and raw:
        import base64

        return base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _decode_page_token(token: str | None) -> bytes | None:
    if not token:
        return None
    import base64

    try:
        return base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None


async def list_workflows(
    *,
    query: str = "",
    page_size: int = 50,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List workflows (Visibility query). Default: running on the control queue."""
    client = await _client()
    if client is None:
        return {"enabled": False, "items": [], "next_page_token": None}
    q = (query or "").strip()
    # Default: open runs on our control queue (closed FAILED/TERMINATED noise is opt-in).
    if not q:
        q = (
            f'TaskQueue = "{temporal_task_queue()}" '
            f'AND ExecutionStatus = "Running"'
        )
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    size = min(max(page_size, 1), 100)
    try:
        pager = client.list_workflows(
            query=q,
            page_size=size,
            next_page_token=_decode_page_token(page_token),
        )
        async for exec in pager:
            items.append(_workflow_summary(exec))
            if len(items) >= size:
                break
        raw = getattr(pager, "next_page_token", None) or getattr(pager, "_next_page_token", None)
        next_token = _encode_page_token(raw)
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
            label = _event_type_label(et)
            events.append(
                {
                    "event_id": getattr(event, "event_id", None),
                    "event_type": label,
                    "summary": _event_summary(event, label),
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
