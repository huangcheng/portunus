"""Portunus error codes (spec §6.4)."""

ERROR_CODES: frozenset[str] = frozenset(
    {
        "unauthorized",
        "revoked",
        "not_granted",
        "server_disabled",
        "tool_disabled",
        "tool_missing",
        "schema_mismatch",
        "payload_too_large",
        "upstream_timeout",
        "upstream_crash",
        "cancelled",
        "auth_required",
        "child_cap",
        "rate_limited",
        "runner_unavailable",
        "spawn_rejected",
        "spawn_failed",
        "unsupported",
    }
)


class PortunusError(Exception):
    """Base error carrying a spec §6.4 code."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)
