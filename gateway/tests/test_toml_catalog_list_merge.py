"""list_apps merges TOML recipes with catalog_apps ids (mode A)."""

from __future__ import annotations

from pathlib import Path

import pytest

from portunus_gateway.toml_catalog import load_apps_dir, set_loaded


@pytest.mark.anyio
async def test_list_apps_prefers_toml_when_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTUNUS_CATALOG_MODE", "A")
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "httpapp.toml").write_text(
        'slug = "httpapp"\n'
        'label = "HTTP App"\n'
        'description = "d"\n'
        'connection = "http"\n'
        "popular = false\n"
        "categories = []\n"
        "tags = []\n"
        "sort_order = 5\n"
        "\n"
        "[spec]\n"
        'url = "https://example.com/mcp"\n',
        encoding="utf-8",
    )
    set_loaded(load_apps_dir(apps))

    from portunus_gateway.catalog import CatalogStore
    from portunus_gateway.db.models import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = CatalogStore(sf)
    apps_list = await store.list_apps()
    slugs = [a["slug"] for a in apps_list]
    assert "httpapp" in slugs
    row = next(a for a in apps_list if a["slug"] == "httpapp")
    assert row["id"] is None  # no catalog_apps row yet
    assert row["catalog_mode"] == "A"
    assert row["label"] == "HTTP App"
    set_loaded(None)
    await engine.dispose()


@pytest.mark.anyio
async def test_list_apps_hides_delisted_toml_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale catalog/apps/qqmail.toml must not resurrect a delisted card."""
    monkeypatch.setenv("PORTUNUS_CATALOG_MODE", "A")
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "qqmail.toml").write_text(
        'slug = "qqmail"\n'
        'label = "QQ Mail"\n'
        'description = "stale official mcp"\n'
        'connection = "oauth"\n'
        "popular = true\n"
        "categories = []\n"
        "tags = []\n"
        "sort_order = 1\n"
        "\n"
        "[spec]\n"
        'url = "https://api.mail.qq.com/mcp"\n'
        'authorize_url = "https://wx.mail.qq.com/oauth/authorize"\n'
        'token_url = "https://wx.mail.qq.com/oauth/token"\n'
        "scopes = [\"mail:read\"]\n",
        encoding="utf-8",
    )
    (apps / "qqmail-imap.toml").write_text(
        'slug = "qqmail-imap"\n'
        'label = "QQ Mail"\n'
        'description = "himalaya"\n'
        'connection = "http"\n'
        "popular = true\n"
        "categories = []\n"
        "tags = []\n"
        "sort_order = 2\n"
        "\n"
        "[spec]\n"
        'url = "https://example.invalid/mcp"\n',
        encoding="utf-8",
    )
    set_loaded(load_apps_dir(apps))

    from portunus_gateway.catalog import CatalogStore
    from portunus_gateway.db.models import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = CatalogStore(sf)
    apps_list = await store.list_apps()
    slugs = [a["slug"] for a in apps_list]
    assert "qqmail" not in slugs
    assert "qqmail-imap" in slugs
    set_loaded(None)
    await engine.dispose()
