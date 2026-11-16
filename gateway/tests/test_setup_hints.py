"""setup_hints: validate_spec passthrough, caps, https links, TOML skill_bridge carry-through."""

from __future__ import annotations

import pytest

from portunus_gateway.catalog import (
    SETUP_HINTS_MAX,
    SETUP_HINTS_MAX_CHARS,
    DEFAULT_APPS,
    RegistryError,
    validate_spec,
)
from portunus_gateway.toml_catalog import parse_recipe


def test_setup_hints_passthrough_all_connections(tmp_path):
    hint = "Paste a token from [console](https://example.com/console)."
    cases = [
        (
            "http",
            {"url": "https://mcp.example.com/mcp", "setup_hints": [hint]},
        ),
        (
            "oauth",
            {
                "url": "https://mcp.example.com/mcp",
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "scopes": ["openid"],
                "setup_hints": [hint],
            },
        ),
        (
            "webdav",
            {
                "root_url": "https://dav.example.com/",
                "root_path": "/",
                "setup_hints": [hint],
            },
        ),
        (
            "stdio",
            {
                "command": "npx",
                "args": ["-y", "demo"],
                "env": [],
                "setup_hints": [hint],
            },
        ),
    ]
    for connection, spec in cases:
        out = validate_spec(connection, spec)
        assert out["setup_hints"] == [hint]


def test_setup_hints_skill_bridge():
    from portunus_gateway.catalog import CUBOX_MANIFEST

    out = validate_spec(
        "skill_bridge",
        {
            "manifest": CUBOX_MANIFEST,
            "secrets": [{"name": "CUBOX_TOKEN", "label": "token"}],
            "setup_hints": ["Enable API at [cubox](https://cubox.pro/web/settings/extensions)."],
        },
    )
    assert out["setup_hints"][0].startswith("Enable API")


def test_setup_hints_reject_http_and_javascript_links():
    base = {"url": "https://mcp.example.com/mcp"}
    with pytest.raises(RegistryError, match="https://"):
        validate_spec(
            "http",
            {**base, "setup_hints": ["See [x](http://insecure.example)."]},
        )
    with pytest.raises(RegistryError, match="https://"):
        validate_spec(
            "http",
            {**base, "setup_hints": ["See [x](javascript:alert(1))."]},
        )


def test_setup_hints_caps():
    base = {"url": "https://mcp.example.com/mcp"}
    with pytest.raises(RegistryError, match="at most"):
        validate_spec(
            "http",
            {**base, "setup_hints": [f"h{i}" for i in range(SETUP_HINTS_MAX + 1)]},
        )
    with pytest.raises(RegistryError, match="≤"):
        validate_spec(
            "http",
            {**base, "setup_hints": ["x" * (SETUP_HINTS_MAX_CHARS + 1)]},
        )


def test_setup_hints_absent_omitted():
    out = validate_spec("http", {"url": "https://mcp.example.com/mcp"})
    assert "setup_hints" not in out


def test_seed_apps_with_hints_validate():
    hinted = [a for a in DEFAULT_APPS if (a.get("spec") or {}).get("setup_hints")]
    assert len(hinted) >= 8
    for a in hinted:
        out = validate_spec(a["connection"], a["spec"])
        assert out.get("setup_hints")


def test_toml_skill_bridge_carries_setup_hints(tmp_path):
    """skill_bridge rebuild must not drop setup_hints before validate_spec."""
    from pathlib import Path

    apps = Path(__file__).resolve().parents[2] / "catalog" / "apps"
    cubox = parse_recipe(apps / "cubox.toml")
    assert cubox.spec.get("setup_hints")
    ima = parse_recipe(apps / "ima.toml")
    assert ima.spec.get("setup_hints")


def test_toml_http_and_oauth_hints():
    from pathlib import Path

    apps = Path(__file__).resolve().parents[2] / "catalog" / "apps"
    for slug in ("youdaonote", "weknora", "github", "mega"):
        recipe = parse_recipe(apps / f"{slug}.toml")
        assert recipe.spec.get("setup_hints"), slug
