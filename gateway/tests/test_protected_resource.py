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
    assert doc is not None
    assert doc["resource"] == "https://portunus.example.com/mcp"
    assert doc["bearer_methods_supported"] == ["header"]
    assert "authorization_servers" not in doc  # operator-issued keys, no OAuth AS
    meta = resource_metadata_url({})
    assert meta == "https://portunus.example.com/.well-known/oauth-protected-resource"
    challenge = bearer_challenge_headers({})
    assert 'resource_metadata="' + meta + '"' in challenge["WWW-Authenticate"]


def test_challenge_ignores_untrusted_forwarded_host(monkeypatch):
    # Without PORTUNUS_PUBLIC_URL / allowlist, Host / X-Forwarded-* must not mint
    # a forgeable resource_metadata URL.
    monkeypatch.delenv("PORTUNUS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PORTUNUS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("PORTUNUS_DEV", raising=False)
    from portunus_gateway.origin import clear_origin_cache

    clear_origin_cache()
    headers = {"x-forwarded-host": "attacker.example", "x-forwarded-proto": "https"}
    challenge = bearer_challenge_headers(headers)
    assert "resource_metadata=" not in challenge["WWW-Authenticate"]
    assert protected_resource_document(headers) is None


def test_challenge_accepts_allowlisted_forwarded_host(monkeypatch):
    monkeypatch.delenv("PORTUNUS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PORTUNUS_ALLOWED_ORIGINS", "https://portunus.example.com")
    monkeypatch.delenv("PORTUNUS_DEV", raising=False)
    from portunus_gateway.origin import clear_origin_cache

    clear_origin_cache()
    headers = {"x-forwarded-host": "portunus.example.com", "x-forwarded-proto": "https"}
    challenge = bearer_challenge_headers(headers)
    assert "resource_metadata=" in challenge["WWW-Authenticate"]
    doc = protected_resource_document(headers)
    assert doc is not None
    assert doc["resource"] == "https://portunus.example.com/mcp"
    # Chained proxies append hosts; mixed case and the default port still match.
    chained = {
        "x-forwarded-host": "Portunus.Example.com:443, internal.example",
        "x-forwarded-proto": "HTTPS, http",
    }
    chained_doc = protected_resource_document(chained)
    assert chained_doc is not None
    assert chained_doc["resource"] == "https://portunus.example.com/mcp"
    assert 'resource_metadata="https://portunus.example.com/' in bearer_challenge_headers(chained)[
        "WWW-Authenticate"
    ]
