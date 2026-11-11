"""Portunus defaults (spec §12, §6.1, §5)."""

from dataclasses import dataclass

# Skill-bridge reads its JSON manifest from this env (gateway injects at start/probe).
# It is the only PORTUNUS_* name allowed through gateway/runner validate_env.
SKILL_BRIDGE_MANIFEST_ENV = "PORTUNUS_MANIFEST"
ALLOWED_PORTUNUS_ENV = frozenset({SKILL_BRIDGE_MANIFEST_ENV})


@dataclass(frozen=True)
class Defaults:
    invoke_timeout_s: int = 60
    start_ack_s: int = 15
    grace_s: int = 30
    heartbeat_s: int = 10
    max_children_per_key: int = 32
    max_children_global: int = 256
    max_probes: int = 2
    probe_timeout_s: int = 60


DEFAULTS = Defaults()
