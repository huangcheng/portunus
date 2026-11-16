"""Origin allowlist on ``/mcp`` (MCP 2026-07-28 Streamable HTTP MUST)."""

from __future__ import annotations

import os

import pytest
from starlette.types import Message

from portunus_gateway.origin import allowed_origins, clear_origin_cache, origin_allowed
from portunus_gateway.omni import _Gate


@pytest.fixture(autouse=True)
def _reset_origin_env(monkeypatch):
    clear_origin_cache()
    monkeypatch.delenv("PORTUNUS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("PORTUNUS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PORTUNUS_DEV", raising=False)
    yield
    clear_origin_cache()


def test_missing_origin_allowed():
    assert origin_allowed(None) is True
    assert origin_allowed("") is True


def test_present_origin_requires_allowlist(monkeypatch):
    monkeypatch.setenv("PORTUNUS_PUBLIC_URL", "https://portunus.example.com")
    clear_origin_cache()
    assert origin_allowed("https://evil.example") is False
    assert origin_allowed("https://portunus.example.com") is True


def test_origin_folds_case_and_default_port(monkeypatch):
    from portunus_gateway.origin import normalize_origin

    assert normalize_origin("https://Example.COM:443/ignored") == "https://example.com"
    assert normalize_origin("http://localhost:80") == "http://localhost"
    assert normalize_origin("http://localhost:8080") == "http://localhost:8080"
    assert normalize_origin("https://[::1]:443") == "https://[::1]"
    assert normalize_origin("http://[::1]:8080") == "http://[::1]:8080"
    assert normalize_origin("https://example.com:notaport") is None
    monkeypatch.setenv("PORTUNUS_PUBLIC_URL", "https://Portunus.Example.com:443")
    clear_origin_cache()
    assert origin_allowed("https://portunus.example.com") is True
    assert origin_allowed("https://Portunus.Example.com:443") is True


def test_explicit_allowed_origins(monkeypatch):
    monkeypatch.setenv(
        "PORTUNUS_ALLOWED_ORIGINS",
        "https://a.example, http://localhost:8080",
    )
    clear_origin_cache()
    assert "https://a.example" in allowed_origins()
    assert origin_allowed("https://a.example") is True
    assert origin_allowed("http://localhost:8080") is True


@pytest.mark.anyio
async def test_gate_rejects_invalid_origin(monkeypatch):
    monkeypatch.setenv("PORTUNUS_PUBLIC_URL", "https://portunus.example.com")
    clear_origin_cache()

    class _Mgr:
        async def handle_request(self, scope, receive, send):
            raise AssertionError("manager must not run when Origin is invalid")

    gate = _Gate(_Mgr())  # type: ignore[arg-type]
    sent: list[Message] = []

    async def send(msg: Message) -> None:
        sent.append(msg)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"origin", b"https://evil.example"),
            (b"authorization", b"Bearer pm-x"),
        ],
    }
    await gate(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 403
