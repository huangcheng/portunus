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


def _norm_event_key(label: str) -> str:
    return label.lower().replace("_", "")


def _payload_preview(payloads: Any, *, limit: int = 120) -> str | None:
    """Decode Temporal payloads into a short operator-facing string."""
    if payloads is None:
        return None
    # History attrs expose a Payloads message; the converter wants the repeated field.
    if hasattr(payloads, "payloads"):
        payloads = list(payloads.payloads)
    if not payloads:
        return None
    try:
        from temporalio.converter import DataConverter

        values = DataConverter.default.payload_converter.from_payloads(payloads)
    except Exception:  # noqa: BLE001
        return None
    if not values:
        return None
    if len(values) == 1:
        raw = values[0]
    else:
        raw = values
    try:
        text = raw if isinstance(raw, str) else repr(raw)
    except Exception:  # noqa: BLE001
        return None
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _failure_message(failure: Any) -> str | None:
    if failure is None:
        return None
    # Prefer the protobuf/API message field (sync). decode_failure is async and
    # not worth awaiting on the history walk path.
    msg = getattr(failure, "message", None)
    if msg:
        return " ".join(str(msg).split())[:240] or None
    try:
        return " ".join(str(failure).split())[:240] or None
    except Exception:  # noqa: BLE001
        return None


def _event_summary(event: Any, label: str) -> str | None:
    """Short secondary line: signal name, activity type, timer id, etc."""
    key = _norm_event_key(label)
    try:
        if "signaled" in key:
            attrs = getattr(event, "workflow_execution_signaled_event_attributes", None)
            name = getattr(attrs, "signal_name", None) if attrs else None
            return f"signal {name}" if name else None
        if "activitytaskscheduled" in key:
            attrs = getattr(event, "activity_task_scheduled_event_attributes", None)
            at = getattr(attrs, "activity_type", None) if attrs else None
            name = getattr(at, "name", None) if at else None
            preview = _payload_preview(getattr(attrs, "input", None)) if attrs else None
            if name and preview:
                return f"{name} · {preview}"
            return name
        if "activitytaskstarted" in key:
            attrs = getattr(event, "activity_task_started_event_attributes", None)
            attempt = getattr(attrs, "attempt", None) if attrs else None
            return f"attempt {attempt}" if attempt else "started"
        if "activitytaskcompleted" in key:
            return "completed"
        if "activitytaskfailed" in key:
            attrs = getattr(event, "activity_task_failed_event_attributes", None)
            return _failure_message(getattr(attrs, "failure", None) if attrs else None) or "failed"
        if "activitytasktimedout" in key:
            return "timed out"
        if "timerstarted" in key:
            attrs = getattr(event, "timer_started_event_attributes", None)
            tid = getattr(attrs, "timer_id", None) if attrs else None
            return f"timer {tid}" if tid else None
        if "markerrecorded" in key:
            attrs = getattr(event, "marker_recorded_event_attributes", None)
            name = getattr(attrs, "marker_name", None) if attrs else None
            return name
        if "workflowexecutioncompleted" in key:
            return "workflow completed"
        if "workflowexecutionfailed" in key:
            attrs = getattr(event, "workflow_execution_failed_event_attributes", None)
            return _failure_message(getattr(attrs, "failure", None) if attrs else None)
    except Exception:  # noqa: BLE001
        return None
    return None


def humanize_query_status(status: str | None) -> str | None:
    """Turn workflow query status strings into a short operator sentence."""
    if not status:
        return None
    raw = status.strip()
    if not raw:
        return None
    if raw == "init":
        return "Starting"
    if raw == "scanning":
        return "Scanning for due work"
    if raw == "paused":
        return "Paused"
    if raw == "probing":
        return "Probing server"
    if raw == "enabling":
        return "Enabling server and tools"
    if raw == "await_oauth":
        return "Waiting for OAuth"
    if raw == "auth_required":
        return "Stopped — auth required"
    if raw == "starting_children":
        return "Starting child workflows"
    if raw == "done":
        return "Finished"
    if raw.startswith("probing:"):
        return f"Probing {raw.split(':', 1)[1]}"
    if raw.startswith("refreshing:"):
        return f"Refreshing OAuth for {raw.split(':', 1)[1]}"
    if raw.startswith("sleep:"):
        # sleep:300 due=2 fail=0
        rest = raw[len("sleep:") :]
        parts = rest.split()
        seconds = parts[0] if parts else "?"
        due = fail = None
        for part in parts[1:]:
            if part.startswith("due="):
                due = part[4:]
            elif part.startswith("fail="):
                fail = part[5:]
        out = f"Sleeping {seconds}s until next wave"
        bits = []
        if due is not None:
            bits.append(f"{due} due")
        if fail is not None:
            bits.append(f"{fail} failed")
        if bits:
            out += " · last wave: " + ", ".join(bits)
        return out
    return raw


_ACTIVITY_ATTRS = {
    "activitytaskstarted": "activity_task_started_event_attributes",
    "activitytaskcompleted": "activity_task_completed_event_attributes",
    "activitytaskfailed": "activity_task_failed_event_attributes",
    "activitytasktimedout": "activity_task_timed_out_event_attributes",
    "activitytaskcanceled": "activity_task_canceled_event_attributes",
}


def tasks_from_history_events(raw_events: list[Any]) -> list[dict[str, Any]]:
    """Collapse activity lifecycle events into operator-facing task rows."""
    by_sched: dict[int, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for event in raw_events:
        et = getattr(event, "event_type", None)
        label = _event_type_label(et)
        key = _norm_event_key(label)
        when = _dt(getattr(event, "event_time", None))
        if "activitytaskscheduled" in key:
            attrs = getattr(event, "activity_task_scheduled_event_attributes", None)
            at = getattr(attrs, "activity_type", None) if attrs else None
            name = getattr(at, "name", None) if at else None
            sched_id = int(getattr(event, "event_id", 0) or 0)
            task = {
                "scheduled_event_id": sched_id,
                "activity_id": getattr(attrs, "activity_id", None) if attrs else None,
                "name": name or "activity",
                "status": "scheduled",
                "attempt": None,
                "input": _payload_preview(getattr(attrs, "input", None)) if attrs else None,
                "error": None,
                "scheduled_at": when,
                "started_at": None,
                "completed_at": None,
            }
            by_sched[sched_id] = task
            ordered.append(task)
            continue
        matched = next((k for k in _ACTIVITY_ATTRS if k in key), None)
        if not matched:
            continue
        attrs = getattr(event, _ACTIVITY_ATTRS[matched], None)
        sched_id = int(getattr(attrs, "scheduled_event_id", 0) or 0) if attrs else 0
        task = by_sched.get(sched_id)
        if task is None:
            continue
        if matched == "activitytaskstarted":
            # A new attempt replaces the previous attempt's terminal state.
            task["status"] = "running"
            task["started_at"] = when
            task["error"] = None
            task["completed_at"] = None
            attempt = getattr(attrs, "attempt", None)
            if attempt is not None:
                task["attempt"] = int(attempt)
        elif matched == "activitytaskcompleted":
            task["status"] = "completed"
            task["completed_at"] = when
            task["error"] = None
        elif matched == "activitytaskfailed":
            task["status"] = "failed"
            task["completed_at"] = when
            task["error"] = _failure_message(getattr(attrs, "failure", None))
        elif matched == "activitytasktimedout":
            task["status"] = "timed_out"
            task["completed_at"] = when
            task["error"] = _failure_message(getattr(attrs, "failure", None)) or "timed out"
        elif matched == "activitytaskcanceled":
            task["status"] = "canceled"
            task["completed_at"] = when
    return ordered


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
    """Describe one workflow and return tasks + truncated event history for the drawer."""
    client = await _client()
    if client is None:
        raise RuntimeError("TEMPORAL_HOST is not configured")
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    summary = _workflow_summary(desc)
    # Query status / stats when the workflow exposes them (OAuth / probe / connect).
    query_status: str | None = None
    try:
        raw_status = await handle.query("status")
        if raw_status is not None:
            query_status = str(raw_status)
    except Exception:  # noqa: BLE001 — workflow may not define status
        query_status = None
    stats: dict[str, Any] | None = None
    try:
        raw_stats = await handle.query("stats")
        if isinstance(raw_stats, dict):
            stats = raw_stats
    except Exception:  # noqa: BLE001 — workflow may not define stats
        stats = None

    events: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    history_truncated = False
    try:
        from collections import deque

        # Walk the full run so task status is current; only keep the newest events
        # for the raw history panel (long-lived loops can have large histories).
        recent_raw: deque[Any] = deque(maxlen=max(history_limit, 1))
        all_raw: list[Any] = []
        total_events = 0
        async for event in handle.fetch_history_events():
            total_events += 1
            recent_raw.append(event)
            all_raw.append(event)
            # Cap memory on pathological histories without splitting an activity
            # lifecycle (keep from a Scheduled event boundary when trimming).
            if len(all_raw) > 5000:
                cut = len(all_raw) - 4000
                while cut < len(all_raw):
                    et = getattr(all_raw[cut], "event_type", None)
                    if "activitytaskscheduled" in _norm_event_key(_event_type_label(et)):
                        break
                    cut += 1
                all_raw = all_raw[cut:]
        history_truncated = total_events > len(recent_raw)
        for event in recent_raw:
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
        # Keep the most recent activity tasks (long-running loops schedule many).
        all_tasks = tasks_from_history_events(all_raw)
        tasks = all_tasks[-40:]
    except Exception as e:  # noqa: BLE001
        return {
            "enabled": True,
            "workflow": summary,
            "query_status": query_status,
            "phase": humanize_query_status(query_status),
            "stats": stats,
            "tasks": tasks,
            "events": events,
            "history_truncated": history_truncated,
            "error": str(e),
        }

    # Surface failure message from describe when closed as failed.
    failure: str | None = None
    close_status = summary.get("status")
    if close_status and "FAILED" in str(close_status).upper():
        # Prefer the latest failed activity error when we have one.
        for task in reversed(tasks):
            if task.get("status") == "failed" and task.get("error"):
                failure = str(task["error"])
                break
        if failure is None:
            for ev in reversed(events):
                if "Failed" in str(ev.get("event_type") or "") and ev.get("summary"):
                    failure = str(ev["summary"])
                    break
        if failure is None:
            failure = "Workflow failed — see tasks / history"

    return {
        "enabled": True,
        "workflow": summary,
        "query_status": query_status,
        "phase": humanize_query_status(query_status),
        "stats": stats,
        "tasks": tasks,
        "events": events,
        "history_truncated": history_truncated,
        "failure": failure,
    }
