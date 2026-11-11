"""RFC 9728 Protected Resource Metadata for ``/mcp``."""

from __future__ import annotations

from portunus_gateway.protected_resource import (
    bearer_challenge_headers,
    protected_resource_document,
    resource_metadata_url,
)


def test_document_and_challenge_use_public_url(monkeypatch):
    monkeypatch.setenv("PORTUNUS_PUBLIC_URL", "https://portunus.example.com")
    doc = protected_resource_document({})
    assert doc["resource"] == "https://portunus.example.com/mcp"
    assert doc["bearer_methods_supported"] == ["header"]
    assert "authorization_servers" not in doc  # operator-issued keys, no OAuth AS
    meta = resource_metadata_url({})
    assert meta == "https://portunus.example.com/.well-known/oauth-protected-resource"
    challenge = bearer_challenge_headers({})
    assert 'resource_metadata="' + meta + '"' in challenge["WWW-Authenticate"]


def test_challenge_falls_back_to_forwarded_host(monkeypatch):
    monkeypatch.delenv("PORTUNUS_PUBLIC_URL", raising=False)
    headers = {"x-forwarded-host": "portunus.example.com", "x-forwarded-proto": "https"}
    challenge = bearer_challenge_headers(headers)
    assert "resource_metadata=" in challenge["WWW-Authenticate"]
    doc = protected_resource_document(headers)
    assert doc["resource"] == "https://portunus.example.com/mcp"
