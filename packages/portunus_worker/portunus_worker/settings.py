"""Env for Temporal client + worker."""

from __future__ import annotations

import os

from . import NAMESPACE, PROBE_TASK_QUEUE, TASK_QUEUE


def temporal_host() -> str:
    return os.environ.get("TEMPORAL_HOST", "").strip()


def temporal_namespace() -> str:
    return os.environ.get("TEMPORAL_NAMESPACE", NAMESPACE).strip() or NAMESPACE


def temporal_task_queue() -> str:
    return os.environ.get("TEMPORAL_TASK_QUEUE", TASK_QUEUE).strip() or TASK_QUEUE


def temporal_probe_task_queue() -> str:
    return os.environ.get("TEMPORAL_PROBE_TASK_QUEUE", PROBE_TASK_QUEUE).strip() or PROBE_TASK_QUEUE


def temporal_enabled() -> bool:
    return bool(temporal_host())


def oauth_refresh_skew_s() -> int:
    raw = os.environ.get("PORTUNUS_OAUTH_REFRESH_SKEW_S", "300")
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


def probe_interval_s() -> int:
    raw = os.environ.get("PORTUNUS_PROBE_INTERVAL_S", "900")
    try:
        return max(60, int(raw))
    except ValueError:
        return 900


def fleet_hosts() -> list[str]:
    raw = os.environ.get("PORTUNUS_FLEET_HOSTS", "")
    return [h for h in raw.split() if h.strip()]


# Default Sync fleet components (workflow + activity reporting must stay aligned).
FLEET_DEFAULT_COMPONENTS = ("rclone", "skills", "clis", "skill_bridge")


def fleet_providers_interval_s() -> int:
    """How often the durable fleet-providers-update loop runs (default: 1 day)."""
    raw = os.environ.get("PORTUNUS_FLEET_PROVIDERS_INTERVAL_S", "86400")
    try:
        return max(60, int(raw))
    except ValueError:
        return 86400
