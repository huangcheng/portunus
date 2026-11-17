"""FleetProvidersUpdateWorkflow helpers."""

from portunus_worker.activities.fleet import parse_component_status
from portunus_worker.client import humanize_query_status
from portunus_worker.workflows.fleet_providers import normalize_components


def test_parse_component_status():
    out = parse_component_status(
        "noise\nCOMPONENT_STATUS rclone=skip skills=ok clis=ok skill_bridge=ok\n"
    )
    assert out == {
        "rclone": "skip",
        "skills": "ok",
        "clis": "ok",
        "skill_bridge": "ok",
    }
    assert parse_component_status("no status") == {}


def test_normalize_components_default():
    assert normalize_components(None) == ["rclone", "skills", "clis", "skill_bridge"]
    assert normalize_components([]) == ["rclone", "skills", "clis", "skill_bridge"]


def test_normalize_components_aliases_and_dedupe():
    assert normalize_components(["Rclone", "tools", "skill-bridge", "skills"]) == [
        "rclone",
        "clis",
        "skill_bridge",
        "skills",
    ]
    assert normalize_components(["bridge"]) == ["skill_bridge"]
    assert normalize_components(["cli"]) == ["clis"]
    assert normalize_components(["vendor-clis", "vendor_clis"]) == ["clis"]


def test_normalize_components_ignores_unknown():
    assert normalize_components(["nope", "rclone"]) == ["rclone"]


def test_humanize_providers_status():
    assert humanize_query_status("host:runner-a") == "Syncing fleet on runner-a"
    assert humanize_query_status("no_hosts") == "No fleet hosts configured"
    assert humanize_query_status("failed") == "Finished with failures"
