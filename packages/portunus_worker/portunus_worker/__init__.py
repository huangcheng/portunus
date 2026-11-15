"""Portunus Temporal control-plane worker."""

__all__ = ["TASK_QUEUE", "PROBE_TASK_QUEUE", "NAMESPACE"]

TASK_QUEUE = "portunus-control"
# Probe activities run in the gateway (sole-client RunnerPool); separate queue so
# the standalone worker never receives probe_server tasks.
PROBE_TASK_QUEUE = "portunus-probe"
# auto-setup image registers ``default``; override with TEMPORAL_NAMESPACE when custom.
NAMESPACE = "default"
