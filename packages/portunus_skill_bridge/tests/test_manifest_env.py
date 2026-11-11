import json
import os

from portunus_skill_bridge.manifest import Manifest
from portunus_skill_bridge.__main__ import _load_from_args


class _Args:
    def __init__(self, *, manifest=None, manifest_env=None, manifest_stdin=False):
        self.manifest = manifest
        self.manifest_env = manifest_env
        self.manifest_stdin = manifest_stdin


def test_load_manifest_from_env(monkeypatch):
    body = {
        "name": "weread",
        "backend": "http_rpc",
        "auth": {"type": "bearer_env", "env": "WEREAD_API_KEY"},
        "http": {"base_url": "https://example.com/gw"},
        "tools": [{"name": "ping", "dispatch": "/ping", "input": {}}],
    }
    monkeypatch.setenv("PORTUNUS_MANIFEST", json.dumps(body))
    m = _load_from_args(_Args(manifest_env="PORTUNUS_MANIFEST"))
    assert isinstance(m, Manifest)
    assert m.name == "weread"
    assert m.tools[0].dispatch == "/ping"
