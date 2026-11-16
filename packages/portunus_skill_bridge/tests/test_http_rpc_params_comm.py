"""http_rpc params_comm body envelope + wire rename (QQ Music)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portunus_skill_bridge.backends.http_rpc import HttpRpcBackend
from portunus_skill_bridge.manifest import load_manifest

QQMUSIC = Path(__file__).resolve().parents[1] / "manifests" / "qqmusic.example.yaml"


@pytest.mark.anyio
async def test_qqmusic_params_comm_and_wire(monkeypatch):
    monkeypatch.setenv("QQMUSIC_API_KEY", "qmk-test")
    m = load_manifest(QQMUSIC)
    backend = HttpRpcBackend(m)
    tool = next(t for t in m.tools if t.name == "discover_search")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_success = True
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"songs": []}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("portunus_skill_bridge.backends.http_rpc.httpx.AsyncClient", return_value=mock_client):
        out = await backend.call(
            tool,
            {"keyword": "周杰伦", "search_type": "0", "page": 1},
        )

    assert out == {"songs": []}
    args, kwargs = mock_client.request.await_args
    assert args[0] == "POST"
    assert args[1] == "https://a.y.qq.com/discover/search"
    assert kwargs["json"] == {
        "params": {"keyword": "周杰伦", "type": "0", "page": 1},
        "comm": {"skill_version": "0.0.3"},
    }
    assert "Bearer qmk-test" in kwargs["headers"]["Authorization"]


@pytest.mark.anyio
async def test_qqmusic_empty_params(monkeypatch):
    monkeypatch.setenv("QQMUSIC_API_KEY", "qmk-test")
    m = load_manifest(QQMUSIC)
    backend = HttpRpcBackend(m)
    tool = next(t for t in m.tools if t.name == "discover_daily_mix")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_success = True
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"songlist": []}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("portunus_skill_bridge.backends.http_rpc.httpx.AsyncClient", return_value=mock_client):
        await backend.call(tool, {})

    _, kwargs = mock_client.request.await_args
    assert kwargs["json"] == {"params": {}, "comm": {"skill_version": "0.0.3"}}
