"""Portunus Temporal control-plane worker."""

__all__ = ["TASK_QUEUE", "NAMESPACE"]

TASK_QUEUE = "portunus-control"
# auto-setup image registers ``default``; override with TEMPORAL_NAMESPACE when custom.
NAMESPACE = "default"
