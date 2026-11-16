"""Derived server health for discovery and the operator API.

Status is never stored. Callers pass the live facts plus the observation columns on
``servers``; ``derive_status`` / ``health_fields`` return the same vocabulary everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SUCCESS_THROTTLE_S = 60.0

# Outcomes that update last_error_* (spec §5). ``upstream_timeout`` only when the caller
# did not shorten the gateway default budget.
FAILURE_CODES = frozenset(
    {
        "auth_required",
        "runner_unavailable",
        "spawn_rejected",
        "spawn_failed",
        "upstream_crash",
        "upstream_timeout",
    }
)

# Transient capacity noise: do not treat as server unreachability.
_SKIP_RUNNER_MESSAGES = frozenset(
    {
        "previous child for this server/key still resolving",
    }
)

STATUSES = frozenset({"probing", "ok", "auth_required", "unreachable"})


@dataclass(frozen=True)
class HealthFacts:
    kind: str
    oauth: dict | None
    has_authorization: bool
    requires_skill_session: bool
    has_skill_session: bool
    probing: bool
    runner_connected: bool
    last_probe_at: datetime | None
    last_ok_at: datetime | None
    last_error_code: str | None
    last_error_at: datetime | None
    enabled_tool_count: int
    # OAuth grant lifetime (F9): null expires_at = unknown / not applicable.
    token_expires_at: datetime | None = None
    has_refresh_token: bool = False


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _missing_credentials(facts: HealthFacts) -> bool:
    if facts.kind == "remote" and facts.oauth and not facts.has_authorization:
        return True
    if facts.requires_skill_session and not facts.has_skill_session:
        return True
    return False


def derive_status(facts: HealthFacts) -> str:
    """First matching rule wins (spec §6).

    Live traffic writes ``last_ok_at``; treat it as evidence of life so servers
    do not stay ``probing`` forever when the operator never clicked Probe
    (review #23 / compliance 2026-11-12).
    """
    if facts.probing:
        return "probing"
    if (
        facts.last_probe_at is None
        and facts.last_error_at is None
        and facts.last_ok_at is None
    ):
        return "probing"
    if _missing_credentials(facts):
        return "auth_required"
    if facts.kind == "stdio" and not facts.runner_connected:
        return "unreachable"
    err_at = facts.last_error_at
    ok_at = facts.last_ok_at
    if err_at is not None and (ok_at is None or err_at > ok_at):
        if facts.last_error_code == "auth_required":
            return "auth_required"
        return "unreachable"
    return "ok"


def health_fields(facts: HealthFacts) -> dict[str, Any]:
    """Discovery / ``server_view`` health projection (includes OAuth grant lifetime)."""
    tool_count: int | None
    # tool_count stays null until the first successful observation (probe or live ok).
    if facts.last_probe_at is None and facts.last_ok_at is None:
        tool_count = None
    else:
        tool_count = facts.enabled_tool_count
    out: dict[str, Any] = {
        "status": derive_status(facts),
        "tool_count": tool_count,
        "last_probe_at": _iso(facts.last_probe_at),
        "last_ok_at": _iso(facts.last_ok_at),
        "last_error_code": facts.last_error_code,
        "last_error_at": _iso(facts.last_error_at),
        # F9: clients can distinguish "will expire soon" from "already broken".
        "token_expires_at": _iso(facts.token_expires_at),
        "has_refresh_token": bool(facts.has_refresh_token),
    }
    return out


def should_record_failure(code: str, *, message: str = "", caller_shortened_timeout: bool = False) -> bool:
    if code not in FAILURE_CODES:
        return False
    if code == "upstream_timeout" and caller_shortened_timeout:
        return False
    if code == "runner_unavailable" and message in _SKIP_RUNNER_MESSAGES:
        return False
    return True


def should_throttle_success(
    *,
    status_now: str,
    last_ok_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """True when a success write can be skipped (already ok and recent)."""
    if status_now != "ok":
        return False
    if last_ok_at is None:
        return False
    clock = now or datetime.now(timezone.utc)
    # SQLite (and some callers) hand back naive datetimes that mean UTC.
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    if last_ok_at.tzinfo is None:
        last_ok_at = last_ok_at.replace(tzinfo=timezone.utc)
    return (clock - last_ok_at).total_seconds() < SUCCESS_THROTTLE_S
