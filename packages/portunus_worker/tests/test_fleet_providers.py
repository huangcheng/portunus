"""FleetProvidersUpdateWorkflow helpers."""

from portunus_worker.client import humanize_query_status
from portunus_worker.workflows.fleet_providers import normalize_components


def test_normalize_components_default():
    assert normalize_components(None) == ["rclone", "skills", "skill_bridge"]
    assert normalize_components([]) == ["rclone", "skills", "skill_bridge"]


def test_normalize_components_aliases_and_dedupe():
    assert normalize_components(["Rclone", "cli", "skill-bridge", "skills"]) == [
        "rclone",
        "skills",
        "skill_bridge",
    ]
    assert normalize_components(["bridge"]) == ["skill_bridge"]


def test_normalize_components_ignores_unknown():
    assert normalize_components(["nope", "rclone"]) == ["rclone"]


def test_humanize_providers_status():
    assert humanize_query_status("host:runner-a") == "Updating providers on runner-a"
    assert humanize_query_status("no_hosts") == "No fleet hosts configured"
    assert humanize_query_status("failed") == "Finished with failures"
