"""http_rpc url_path + extra_headers_env (IMA OpenAPI)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portunus_skill_bridge.backends.base import resolve_auth_headers
from portunus_skill_bridge.backends.http_rpc import HttpRpcBackend
from portunus_skill_bridge.manifest import Manifest


def _ima_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "name": "ima",
            "backend": "http_rpc",
            "auth": {
                "type": "header_env",
                "env": "IMA_OPENAPI_APIKEY",
                "header": "ima-openapi-apikey",
                "extra_headers_env": {"ima-openapi-clientid": "IMA_OPENAPI_CLIENTID"},
            },
            "http": {
                "base_url": "https://ima.qq.com",
                "dispatch_mode": "url_path",
                "headers": {"ima-openapi-ctx": "skill_version=1.1.10"},
            },
            "tools": [
                {
                    "name": "search_note",
                    "description": "search",
                    "dispatch": "openapi/note/v1/search_note",
                    "input": {},
                }
            ],
        }
    )


def test_resolve_auth_headers_includes_companion(monkeypatch):
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", "key-1")
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", "client-1")
    headers = resolve_auth_headers(_ima_manifest())
    assert headers == {
        "ima-openapi-apikey": "key-1",
        "ima-openapi-clientid": "client-1",
    }


@pytest.mark.anyio
async def test_http_rpc_url_path_posts_to_dispatch(monkeypatch):
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", "key-1")
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", "client-1")
    backend = HttpRpcBackend(_ima_manifest())
    tool = backend.manifest.tools[0]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"code": 0}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("portunus_skill_bridge.backends.http_rpc.httpx.AsyncClient", return_value=mock_client):
        out = await backend.call(tool, {"start": 0, "end": 10})

    assert out == {"code": 0}
    mock_client.request.assert_awaited_once()
    args, kwargs = mock_client.request.await_args
    assert args[0] == "POST"
    assert args[1] == "https://ima.qq.com/openapi/note/v1/search_note"
    assert kwargs["json"] == {"start": 0, "end": 10}
    assert kwargs["headers"]["ima-openapi-apikey"] == "key-1"
    assert kwargs["headers"]["ima-openapi-clientid"] == "client-1"
    assert kwargs["headers"]["ima-openapi-ctx"] == "skill_version=1.1.10"
