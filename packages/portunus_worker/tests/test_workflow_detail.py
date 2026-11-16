"""Pure helpers for workflow detail drawer (phase + activity tasks)."""

from __future__ import annotations

from types import SimpleNamespace

from portunus_worker.client import humanize_query_status, tasks_from_history_events


def test_humanize_query_status():
    assert humanize_query_status("scanning") == "Scanning for due work"
    assert humanize_query_status("probing:abc") == "Probing abc"
    assert humanize_query_status("refreshing:xyz") == "Refreshing OAuth for xyz"
    assert (
        humanize_query_status("sleep:300 due=2 fail=1")
        == "Sleeping 300s until next wave · last wave: 2 due, 1 failed"
    )
    assert humanize_query_status(None) is None


def _ev(event_id: int, etype: str, **attrs):
    ns = SimpleNamespace(event_id=event_id, event_type=etype, event_time=None)
    for key, val in attrs.items():
        setattr(ns, key, val)
    return ns


def test_tasks_from_history_events_lifecycle():
    events = [
        _ev(
            5,
            "ActivityTaskScheduled",
            activity_task_scheduled_event_attributes=SimpleNamespace(
                activity_id="1",
                activity_type=SimpleNamespace(name="probe_server"),
                input=None,
            ),
        ),
        _ev(
            6,
            "ActivityTaskStarted",
            activity_task_started_event_attributes=SimpleNamespace(
                scheduled_event_id=5,
                attempt=1,
            ),
        ),
        _ev(
            7,
            "ActivityTaskCompleted",
            activity_task_completed_event_attributes=SimpleNamespace(
                scheduled_event_id=5,
            ),
        ),
        _ev(
            8,
            "ActivityTaskScheduled",
            activity_task_scheduled_event_attributes=SimpleNamespace(
                activity_id="2",
                activity_type=SimpleNamespace(name="list_probe_plan"),
                input=None,
            ),
        ),
        _ev(
            9,
            "ActivityTaskStarted",
            activity_task_started_event_attributes=SimpleNamespace(
                scheduled_event_id=8,
                attempt=2,
            ),
        ),
    ]
    tasks = tasks_from_history_events(events)
    assert len(tasks) == 2
    assert tasks[0]["name"] == "probe_server"
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["attempt"] == 1
    assert tasks[1]["name"] == "list_probe_plan"
    assert tasks[1]["status"] == "running"
    assert tasks[1]["attempt"] == 2


def test_tasks_from_history_failed():
    events = [
        _ev(
            1,
            "ActivityTaskScheduled",
            activity_task_scheduled_event_attributes=SimpleNamespace(
                activity_id="1",
                activity_type=SimpleNamespace(name="refresh_oauth"),
                input=None,
            ),
        ),
        _ev(
            2,
            "ActivityTaskFailed",
            activity_task_failed_event_attributes=SimpleNamespace(
                scheduled_event_id=1,
                failure=SimpleNamespace(message="token expired"),
            ),
        ),
    ]
    tasks = tasks_from_history_events(events)
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["error"] == "token expired"
