"""skill_bridge catalog connection + manifest validation."""

from __future__ import annotations

import pytest
from portunus_gateway.catalog import (
    CUBOX_MANIFEST,
    GUANGYAPAN_MANIFEST,
    GUANGYAPAN_SKILL_ROOT,
    GUANGYAPAN_STATE_DIR,
    SKILL_BRIDGE_ARGS,
    SKILL_BRIDGE_COMMAND,
    WEREAD_MANIFEST,
    validate_spec,
)
from portunus_gateway.registry import RegistryError
from portunus_shared.argv import ArgvError, validate_stdio_command


def test_validate_weread_skill_bridge_spec():
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": WEREAD_MANIFEST,
            "secrets": [{"name": "WEREAD_API_KEY", "label": "API key"}],
        },
    )
    assert out["command"] == SKILL_BRIDGE_COMMAND
    assert out["args"] == SKILL_BRIDGE_ARGS
    assert out["manifest"]["backend"] == "http_rpc"
    assert out["secrets"][0]["name"] == "WEREAD_API_KEY"


def test_skill_bridge_requires_auth_secret():
    with pytest.raises(RegistryError):
        validate_spec(
            "skill_bridge",
            {"manifest": WEREAD_MANIFEST, "secrets": []},
        )


def test_validate_guangyapan_skill_bridge_spec():
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": GUANGYAPAN_MANIFEST,
            "secrets": [],
            "env_defaults": {
                "SKILL_ROOT": GUANGYAPAN_SKILL_ROOT,
                "STATE_DIR": GUANGYAPAN_STATE_DIR,
            },
        },
    )
    assert out["manifest"]["backend"] == "json_cli"
    assert out["manifest"]["auth"]["type"] == "device_qr"
    assert out["manifest"]["auth"]["login_env"]["GUANGYAPAN_DRIVE_LOGIN_MODE"] == "device"
    assert out["env_defaults"]["SKILL_ROOT"] == GUANGYAPAN_SKILL_ROOT
    assert {t["name"] for t in out["manifest"]["tools"]} >= {"list", "search", "upload", "status"}


def test_argv_skill_bridge_ok(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: f"/usr/bin/{c}")
    validate_stdio_command(
        "python",
        list(SKILL_BRIDGE_ARGS),
        trailing_allowlist=[],
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )


def test_argv_skill_bridge_rejects_other_module(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: f"/usr/bin/{c}")
    with pytest.raises(ArgvError):
        validate_stdio_command(
            "python",
            ["-m", "evil", "--manifest-env", "X"],
            trailing_allowlist=[],
            trusted_path=["/usr/bin"],
            binary_allowlist=[],
        )


@pytest.mark.anyio
async def test_bearer_env_skill_app_not_flagged_auth_required(db, pepper_file):
    """http_rpc apps (bearer_env) have no login session and must not show auth_required.

    Regression: ``requires_skill_session`` used to be true for every skill_bridge slug,
    so WeRead-style API-key apps were permanently ``auth_required`` despite working.
    """
    import os
    import uuid as _uuid

    from portunus_gateway.db.models import CatalogApp, Server, Tool
    from portunus_gateway.db.session import async_session
    from portunus_gateway.registry import Registry
    from portunus_gateway.runner_pool import RunnerPool
    from portunus_gateway.vault import Vault

    async with async_session() as s:
        s.add(
            CatalogApp(
                id=_uuid.uuid4(),
                slug="bearerdrive",
                label="BearerDrive",
                name="BearerDrive",
                description="API-key skill app",
                connection="skill_bridge",
                spec={
                    "manifest": {"auth": {"type": "bearer_env", "env": "K", "header": "Authorization"}},
                    "secrets": [{"name": "K", "label": "Key", "secret": True}],
                    "env_defaults": {},
                },
                sort_order=1,
            )
        )
        sid = _uuid.uuid4()
        srv = Server(
            id=sid, name="BearerDrive", slug="bearerdrive", kind="stdio", enabled=True,
            command="python", args=["-c", "pass"],
        )
        s.add(srv)
        s.add(Tool(server_id=sid, upstream_name="t", schema={}, schema_hash="h"))
        await s.commit()

    reg = Registry(
        async_session,
        Vault({1: os.urandom(32)}),
        RunnerPool([]),
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
    async with async_session() as s:
        server = (await s.execute(__import__("sqlalchemy").select(Server).where(Server.slug == "bearerdrive"))).scalar_one()
    health = await reg.health_by_server_ids([server])
    # No SKILL_SESSION vault entry, no login ever: still not auth_required — the app's
    # auth model does not have a session to require.
    assert health[sid]["status"] != "auth_required", health[sid]


@pytest.mark.anyio
async def test_auth_none_skill_app_not_flagged_auth_required(db, pepper_file):
    """json_cli apps with auth.type=none (Cubox CUBOX_TOKEN) must not show auth_required.

    Regression: only bearer_env/header_env were excluded from requires_skill_session,
    so Cubox stayed permanently Needs auth despite paste-token Connect.
    """
    import os
    import uuid as _uuid

    from portunus_gateway.db.models import CatalogApp, Server, Tool
    from portunus_gateway.db.session import async_session
    from portunus_gateway.registry import Registry
    from portunus_gateway.runner_pool import RunnerPool
    from portunus_gateway.vault import Vault

    async with async_session() as s:
        s.add(
            CatalogApp(
                id=_uuid.uuid4(),
                slug="cuboxish",
                label="Cuboxish",
                name="Cuboxish",
                description="CLI token skill app",
                connection="skill_bridge",
                spec={
                    "manifest": {"auth": {"type": "none"}, "backend": "json_cli", "tools": []},
                    "secrets": [{"name": "CUBOX_TOKEN", "label": "Token", "secret": True}],
                    "env_defaults": {"CUBOX_SERVER": "cubox.pro"},
                },
                sort_order=1,
            )
        )
        sid = _uuid.uuid4()
        srv = Server(
            id=sid,
            name="Cuboxish",
            slug="cuboxish",
            kind="stdio",
            enabled=True,
            command="python",
            args=["-c", "pass"],
        )
        s.add(srv)
        s.add(Tool(server_id=sid, upstream_name="t", schema={}, schema_hash="h"))
        await s.commit()

    reg = Registry(
        async_session,
        Vault({1: os.urandom(32)}),
        RunnerPool([]),
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
    async with async_session() as s:
        server = (
            await s.execute(
                __import__("sqlalchemy").select(Server).where(Server.slug == "cuboxish")
            )
        ).scalar_one()
    health = await reg.health_by_server_ids([server])
    assert health[sid]["status"] != "auth_required", health[sid]


def test_rclone_drive_seeds_shape():
    """Box/Drime/MEGA ship as rclone-shim stdio apps with a multiline config paste."""
    from portunus_gateway.catalog import DEFAULT_APPS, RCLONE_SHIM, validate_spec

    for slug in ("box", "drime", "mega"):
        raw = next(a for a in DEFAULT_APPS if a["slug"] == slug)
        assert raw["connection"] == "stdio"
        spec = validate_spec("stdio", raw["spec"])
        assert spec["command"] == RCLONE_SHIM
        assert spec["args"] == []
        entry = spec["env"][0]
        assert entry["name"] == "RCLONE_REMOTE_CONFIG"
        assert entry["multiline"] is True
        assert entry["secret"] is True


def test_yandex_oauth_mcp_seeds_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, preferred_regions_for_slug, validate_spec

    disk = next(a for a in DEFAULT_APPS if a["slug"] == "yandex")
    mail = next(a for a in DEFAULT_APPS if a["slug"] == "yandex-mail")
    assert disk["connection"] == "oauth"
    assert mail["connection"] == "oauth"
    disk_spec = validate_spec("oauth", disk["spec"])
    mail_spec = validate_spec("oauth", mail["spec"])
    assert disk_spec["url"] == "https://mcp.360.yandex.net/v1/disk/mcp"
    assert mail_spec["url"] == "https://mcp.360.yandex.net/v1/mail/mcp"
    assert "client_id" not in disk_spec
    assert "client_id" not in mail_spec
    assert disk_spec["authorization_scheme"] == "OAuth"
    assert mail_spec["authorization_scheme"] == "OAuth"
    assert disk_spec.get("allow_bearer_token") is True
    assert mail_spec.get("allow_bearer_token") is True
    # Default space join (Yandex docs); do not set comma delimiter.
    assert "scope_delimiter" not in disk_spec
    assert "scope_delimiter" not in mail_spec
    assert disk_spec["token_endpoint_auth_method"] == "client_secret_post"
    assert "cloud_api:disk.read" in disk_spec["scopes"]
    assert "cloud_api:disk.write" in disk_spec["scopes"]
    assert "mail:messages.read" in mail_spec["scopes"]
    assert "cloud_api:disk.read" not in mail_spec["scopes"]
    assert "mail:messages.read" not in disk_spec["scopes"]
    assert preferred_regions_for_slug("yandex") == ["overseas"]
    assert preferred_regions_for_slug("yandex-mail") == ["overseas"]


def test_gdrive_oauth_drive_mcp_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, preferred_regions_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "gdrive")
    assert raw["connection"] == "oauth"
    spec = validate_spec("oauth", raw["spec"])
    assert spec["url"] == "https://drivemcp.googleapis.com/mcp/v1"
    assert "access_type=offline" in spec["authorize_url"]
    assert spec["token_url"] == "https://oauth2.googleapis.com/token"
    assert spec["token_endpoint_auth_method"] == "client_secret_post"
    # accounts.google.com does not return RFC 9207 ``iss``; configuring issuer
    # would make the callback require it and break Connect.
    assert "issuer" not in spec
    assert "https://www.googleapis.com/auth/drive.file" in spec["scopes"]
    assert preferred_regions_for_slug("gdrive") == ["overseas"]


def test_ms_graph_oauth_seeds_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, preferred_regions_for_slug, validate_spec

    mail = next(a for a in DEFAULT_APPS if a["slug"] == "outlook-mail")
    drive = next(a for a in DEFAULT_APPS if a["slug"] == "onedrive")
    assert mail["connection"] == "oauth"
    assert drive["connection"] == "oauth"
    mail_spec = validate_spec("oauth", mail["spec"])
    drive_spec = validate_spec("oauth", drive["spec"])
    assert mail_spec["url"] == "portunus-graph://mail"
    assert drive_spec["url"] == "portunus-graph://onedrive"
    # Self-host OSS: never bake a deploy-owned Entra client_id (paste at Connect).
    assert "client_id" not in mail_spec
    assert "client_id" not in drive_spec
    assert mail_spec["token_endpoint_auth_method"] == "client_secret_post"
    assert "Mail.Send" in mail_spec["scopes"]
    assert "Files.ReadWrite" in drive_spec["scopes"]
    assert "Files.ReadWrite" not in mail_spec["scopes"]
    assert "Mail.Send" not in drive_spec["scopes"]
    assert preferred_regions_for_slug("outlook-mail") == ["overseas"]
    assert preferred_regions_for_slug("onedrive") == ["overseas"]


def test_oauth_seeds_never_bake_client_id():
    """Confidential-client recipes must not ship a Portunus-owned client_id."""
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec

    for raw in DEFAULT_APPS:
        if raw.get("connection") != "oauth":
            continue
        spec = validate_spec("oauth", raw["spec"])
        assert "client_id" not in spec, f"{raw['slug']} bakes client_id={spec.get('client_id')!r}"


def test_egress_preferred_regions_for_catalog_slugs():
    from portunus_gateway.catalog import (
        DOMESTIC_PREFERRED_REGIONS,
        OVERSEAS_PREFERRED_REGIONS,
        egress_for_slug,
        preferred_regions_for_slug,
    )

    for slug in (
        "yandex",
        "yandex-mail",
        "drime",
        "box",
        "onedrive",
        "outlook-mail",
        "thundermail",
        "gdrive",
        "mega",
        "raindrop",
        "github",
        "cloudflare",
        "linear",
        "dropbox",
        "pikpak",
        "Yandex",
        "vivaldi-mail",
    ):
        assert preferred_regions_for_slug(slug) == OVERSEAS_PREFERRED_REGIONS
        assert egress_for_slug(slug) == "overseas"
    for slug in (
        "nutstore",
        "weiyun",
        "guangyapan",
        "cubox",
        "ardot",
        "qqmusic",
        "qqmail-imap",
    ):
        assert preferred_regions_for_slug(slug) == DOMESTIC_PREFERRED_REGIONS
        assert egress_for_slug(slug) == "domestic"
    assert preferred_regions_for_slug("weknora") == []
    assert egress_for_slug("weknora") == "any"
    assert preferred_regions_for_slug("") == []


def test_fastmail_oauth_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "fastmail")
    spec = validate_spec("oauth", raw["spec"])
    assert spec["url"] == "https://api.fastmail.com/mcp"
    assert spec["token_url"] == "https://api.fastmail.com/oauth/refresh"
    # Fastmail rejects unregistered public https redirects for CIMD/DCR.
    assert "use_cimd" not in spec
    assert spec["allow_bearer_token"] is True
    assert "https://www.fastmail.com/dev/mcp" in spec["scopes"]
    assert "offline_access" in spec["scopes"]


def test_aliyun_oauth_core_streamable_http():
    from portunus_gateway.catalog import DEFAULT_APPS, egress_for_slug, validate_spec

    assert not any(a["slug"] == "aliyun-ak" for a in DEFAULT_APPS)
    raw = next(a for a in DEFAULT_APPS if a["slug"] == "aliyun")
    assert raw["connection"] == "oauth"
    assert raw["label"] == "阿里云"
    spec = validate_spec("oauth", raw["spec"])
    assert spec.get("url_from_operator") is True
    assert spec["authorize_url"] == "https://signin.aliyun.com/oauth2/v1/auth"
    assert spec["token_url"] == "https://oauth.aliyun.com/v1/token"
    # Fake DCR must not be advertised — Aliyun ignores custom redirect_uris.
    assert "registration_endpoint" not in spec
    assert "issuer" not in spec  # Aliyun omits RFC 9207 iss on callback
    assert spec["token_endpoint_auth_method"] == "none"
    assert spec["resource"] == "https://openapi-mcp.cn-hangzhou.aliyuncs.com"
    assert spec["scopes"] == ["openid", "/acs/mcp-server"]
    assert egress_for_slug("aliyun") == "domestic"


def test_tencentcloud_delisted_from_default_apps():
    from portunus_gateway.catalog import DEFAULT_APPS, egress_for_slug
    from portunus_gateway.toml_catalog import load_apps_dir, packaged_apps_dir

    assert not any(a["slug"] == "tencentcloud" for a in DEFAULT_APPS)
    # Domestic affinity still applies to any leftover connected server.
    assert egress_for_slug("tencentcloud") == "domestic"
    loaded = load_apps_dir(packaged_apps_dir())
    recipe = next(r for r in loaded.recipes if r.slug == "tencentcloud")
    assert recipe.enabled is False
    assert recipe.disable_reasons == ["disabled_in_toml"]


@pytest.mark.anyio
async def test_aliyun_ensure_defaults_clears_fake_dcr_client():
    """Aliyun /register returns official client_id; wipe it so Connect asks for Custom OAuth."""
    import uuid

    from portunus_gateway.catalog import CatalogStore, DEFAULT_APPS, validate_spec
    from portunus_gateway.db.models import Base, CatalogApp, Server
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    seed = next(a for a in DEFAULT_APPS if a["slug"] == "aliyun")
    want_spec = validate_spec("oauth", seed["spec"])
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async with sf() as s:
        s.add(
            CatalogApp(
                id=uuid.uuid4(),
                slug="aliyun",
                label=seed["label"],
                name=seed["name"],
                description="stale dcr",
                connection="oauth",
                popular=True,
                categories=list(seed.get("categories") or []),
                tags=list(seed.get("tags") or []),
                spec={
                    **want_spec,
                    "registration_endpoint": (
                        "https://openapi-mcp.cn-hangzhou.aliyuncs.com/register"
                    ),
                    "token_endpoint_auth_method": "none",
                    "scopes": ["openid", "/internal/acs/openapi"],
                },
                sort_order=17,
            )
        )
        s.add(
            Server(
                id=uuid.uuid4(),
                name="阿里云",
                slug="aliyun",
                kind="remote",
                url="https://openapi-mcp.cn-hangzhou.aliyuncs.com/accounts/x/id/y/mcp",
                oauth={
                    "authorize_url": want_spec["authorize_url"],
                    "token_url": want_spec["token_url"],
                    "client_id": "4071151845732613353",
                    "issuer": "https://openapi-mcp.cn-hangzhou.aliyuncs.com",
                    "registration_endpoint": (
                        "https://openapi-mcp.cn-hangzhou.aliyuncs.com/register"
                    ),
                    "token_endpoint_auth_method": "none",
                    "scopes": ["openid", "/internal/acs/openapi"],
                },
                recipe_slug="aliyun",
                vault_version=1,
                enabled=True,
            )
        )
        await s.commit()

    store = CatalogStore(sf)
    await store.ensure_defaults()

    async with sf() as s:
        row = (
            await s.execute(select(CatalogApp).where(CatalogApp.slug == "aliyun"))
        ).scalar_one()
        assert row.spec == want_spec
        assert "registration_endpoint" not in row.spec
        assert "issuer" not in row.spec
        srv = (
            await s.execute(select(Server).where(Server.slug == "aliyun"))
        ).scalar_one()
        assert isinstance(srv.oauth, dict)
        assert "client_id" not in srv.oauth
        assert "registration_endpoint" not in srv.oauth
        assert "issuer" not in srv.oauth
        assert srv.oauth.get("token_endpoint_auth_method") == "none"
        assert srv.oauth.get("scopes") == ["openid", "/acs/mcp-server"]

    await engine.dispose()


@pytest.mark.anyio
async def test_aliyun_ensure_defaults_stdio_to_oauth_and_purge():
    """AccessKey stdio aliyun → Core oauth; recipe-bound aliyun-ak purged; custom slug kept."""
    import uuid

    from portunus_gateway.catalog import CatalogStore, DEFAULT_APPS, validate_spec
    from portunus_gateway.db.models import Base, CatalogApp, Server, Tool, VaultEntry
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    seed = next(a for a in DEFAULT_APPS if a["slug"] == "aliyun")
    want_spec = validate_spec("oauth", seed["spec"])
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    recipe_ak_id = uuid.uuid4()
    custom_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            CatalogApp(
                id=uuid.uuid4(),
                slug="aliyun",
                label="阿里云",
                name="阿里云",
                description="old stdio",
                connection="stdio",
                popular=True,
                categories=["Developer Tools"],
                tags=["accesskey"],
                spec={
                    "command": "uvx",
                    "args": ["alibabacloud.mcp-proxy==0.2.17"],
                    "env": [
                        {"name": "ALIBABA_CLOUD_ACCESS_KEY_ID", "secret": True},
                        {"name": "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "secret": True},
                    ],
                },
                sort_order=17,
            )
        )
        aliyun_srv = Server(
            id=uuid.uuid4(),
            name="阿里云",
            slug="aliyun",
            kind="stdio",
            command="uvx",
            args=["alibabacloud.mcp-proxy==0.2.17"],
            recipe_slug="aliyun",
            vault_version=1,
        )
        s.add(aliyun_srv)
        s.add(
            Tool(
                id=uuid.uuid4(),
                server_id=aliyun_srv.id,
                upstream_name="old",
                description=None,
                schema={},
                schema_hash="h",
            )
        )
        s.add(
            VaultEntry(
                id=uuid.uuid4(),
                server_id=aliyun_srv.id,
                name="ALIBABA_CLOUD_ACCESS_KEY_ID",
                ciphertext=b"\x00" * 32,
                key_version=1,
            )
        )
        s.add(
            CatalogApp(
                id=uuid.uuid4(),
                slug="aliyun-ak",
                label="阿里云 AccessKey",
                name="阿里云 AccessKey",
                description="stale",
                connection="stdio",
                popular=False,
                categories=[],
                tags=[],
                spec={
                    "command": "uvx",
                    "args": ["alibabacloud.mcp-proxy==0.2.0"],
                    "env": [],
                },
                sort_order=18,
            )
        )
        s.add(
            Server(
                id=recipe_ak_id,
                name="aliyun-ak catalog",
                slug="aliyun-ak",
                kind="stdio",
                command="uvx",
                args=["alibabacloud.mcp-proxy==0.2.0"],
                recipe_slug="aliyun-ak",
            )
        )
        await s.commit()

    store = CatalogStore(sf)
    await store.ensure_defaults()

    async with sf() as s:
        row = (
            await s.execute(select(CatalogApp).where(CatalogApp.slug == "aliyun"))
        ).scalar_one()
        assert row.connection == "oauth"
        assert row.spec == want_spec
        assert "oauth" in (row.tags or [])
        assert (
            await s.execute(select(CatalogApp).where(CatalogApp.slug == "aliyun-ak"))
        ).scalar_one_or_none() is None
        assert (
            await s.execute(select(Server).where(Server.id == recipe_ak_id))
        ).scalar_one_or_none() is None
        srv = (
            await s.execute(select(Server).where(Server.slug == "aliyun"))
        ).scalar_one()
        assert srv.kind == "remote"
        assert srv.command is None
        assert list(srv.args or []) == []
        assert srv.url is None
        assert srv.oauth is None
        assert srv.has_oauth_refresh is False
        assert srv.enabled is False
        assert int(srv.vault_version) >= 2
        assert (
            await s.execute(select(Tool).where(Tool.server_id == srv.id))
        ).scalars().all() == []
        assert (
            await s.execute(
                select(VaultEntry).where(
                    VaultEntry.server_id == srv.id,
                    VaultEntry.name == "ALIBABA_CLOUD_ACCESS_KEY_ID",
                )
            )
        ).scalar_one_or_none() is None
        # Custom operator server on the freed slug (no recipe) must survive a reboot.
        s.add(
            Server(
                id=custom_id,
                name="custom ak",
                slug="aliyun-ak",
                kind="stdio",
                command="uvx",
                args=["noop"],
                recipe_slug=None,
            )
        )
        await s.commit()

    await store.ensure_defaults()
    async with sf() as s:
        assert (
            await s.execute(select(Server).where(Server.id == custom_id))
        ).scalar_one_or_none() is not None

    await engine.dispose()


@pytest.mark.anyio
async def test_tencentcloud_ensure_defaults_delists_catalog():
    import uuid

    from portunus_gateway.catalog import CatalogStore
    from portunus_gateway.db.models import Base, CatalogApp, Server
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async with sf() as s:
        s.add(
            CatalogApp(
                id=uuid.uuid4(),
                slug="tencentcloud",
                label="腾讯云",
                name="腾讯云",
                description="stale",
                connection="http",
                popular=True,
                categories=["Developer Tools"],
                tags=["tencentcloud"],
                spec={"url_from_operator": True, "url_is_credential": True},
                sort_order=12,
            )
        )
        s.add(
            Server(
                id=uuid.uuid4(),
                name="腾讯云",
                slug="tencentcloud",
                kind="remote",
                url="https://mcp-api.tencent-cloud.com/sse/x",
                recipe_slug="tencentcloud",
                enabled=True,
                vault_version=2,
            )
        )
        await s.commit()

    store = CatalogStore(sf)
    await store.ensure_defaults()

    async with sf() as s:
        assert (
            await s.execute(select(CatalogApp).where(CatalogApp.slug == "tencentcloud"))
        ).scalar_one_or_none() is None
        srv = (
            await s.execute(select(Server).where(Server.slug == "tencentcloud"))
        ).scalar_one()
        assert srv.enabled is False

    await engine.dispose()


def test_infisical_stdio_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, egress_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "infisical")
    assert raw["connection"] == "stdio"
    spec = validate_spec("stdio", raw["spec"])
    assert spec["command"] == "npx"
    assert spec["args"] == ["-y", "--ignore-scripts", "@infisical/mcp@0.1.0"]
    env_by_name = {e["name"]: e for e in spec["env"]}
    assert env_by_name["INFISICAL_HOST_URL"]["secret"] is False
    assert env_by_name["INFISICAL_UNIVERSAL_AUTH_CLIENT_ID"]["secret"] is True
    assert env_by_name["INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"]["secret"] is True
    assert spec["env_defaults"]["INFISICAL_HOST_URL"] == "https://app.infisical.com"
    assert spec["env_defaults"]["INFISICAL_AUTH_METHOD"] == "universal-auth"
    # Self-host can be domestic or overseas.
    assert egress_for_slug("infisical") == "any"


def test_doppler_stdio_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, egress_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "doppler")
    assert raw["connection"] == "stdio"
    spec = validate_spec("stdio", raw["spec"])
    assert spec["command"] == "npx"
    assert spec["args"] == ["-y", "--ignore-scripts", "@dopplerhq/mcp-server@1.0.5"]
    assert any(e["name"] == "DOPPLER_TOKEN" and e["secret"] for e in spec["env"])
    assert egress_for_slug("doppler") == "overseas"


def test_github_oauth_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "github")
    assert raw["connection"] == "oauth"
    spec = validate_spec("oauth", raw["spec"])
    assert spec["url"] == "https://api.githubcopilot.com/mcp/"
    assert spec["authorize_url"] == "https://github.com/login/oauth/authorize"
    assert spec["token_url"] == "https://github.com/login/oauth/access_token"
    assert "registration_endpoint" not in spec  # GitHub remote MCP has no DCR
    assert "repo" in spec["scopes"]
    assert spec["resource"] == "https://api.githubcopilot.com/mcp/"


def test_cloudflare_oauth_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "cloudflare")
    assert raw["connection"] == "oauth"
    spec = validate_spec("oauth", raw["spec"])
    assert spec["url"] == "https://mcp.cloudflare.com/mcp"
    assert spec["registration_endpoint"] == "https://mcp.cloudflare.com/register"
    assert spec["authorize_url"] == "https://mcp.cloudflare.com/authorize"
    assert spec["token_url"] == "https://mcp.cloudflare.com/token"
    assert spec["token_endpoint_auth_method"] == "none"
    assert "offline_access" in spec["scopes"]


def test_linear_oauth_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, egress_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "linear")
    assert raw["connection"] == "oauth"
    assert raw.get("egress") == "overseas"
    assert egress_for_slug("linear") == "overseas"
    spec = validate_spec("oauth", raw["spec"])
    assert spec["url"] == "https://mcp.linear.app/mcp"
    assert spec["registration_endpoint"] == "https://mcp.linear.app/register"
    assert spec["authorize_url"] == "https://mcp.linear.app/authorize"
    assert spec["token_url"] == "https://mcp.linear.app/token"
    assert spec["token_endpoint_auth_method"] == "none"
    assert spec["issuer"] == "https://mcp.linear.app"
    assert spec["resource"] == "https://mcp.linear.app/mcp"
    assert spec["scopes"] == ["read", "write"]


def test_qqmail_imap_himalaya_seed_shape():
    """QQ IMAP provider shares himalaya.example.yaml + himalaya-shim run wrapper."""
    from portunus_gateway.catalog import (
        DEFAULT_APPS,
        HIMALAYA_SHIM_ROOT,
        HIMALAYA_SHIM_RUN,
        egress_for_slug,
        validate_spec,
    )

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "qqmail-imap")
    assert raw["connection"] == "skill_bridge"
    assert raw.get("egress") == "domestic"
    assert egress_for_slug("qqmail-imap") == "domestic"
    spec = validate_spec("skill_bridge", raw["spec"])
    assert spec["manifest"]["name"] == "himalaya"
    assert spec["manifest"]["cli"]["command"] == [HIMALAYA_SHIM_RUN]
    names = {s["name"] for s in spec["secrets"]}
    assert names == {"MAIL_ADDRESS", "MAIL_PASSWORD"}
    assert spec["env_defaults"]["IMAP_URL"] == "imaps://imap.qq.com:993"
    assert spec["env_defaults"]["SMTP_URL"] == "smtps://smtp.qq.com:465"
    assert spec["env_defaults"]["SKILL_ROOT"] == HIMALAYA_SHIM_ROOT
    tool_names = {t["name"] for t in spec["manifest"]["tools"]}
    assert {"mailbox_list", "envelope_list", "message_read", "message_send"} <= tool_names


def test_vivaldi_mail_himalaya_seed_shape():
    from portunus_gateway.catalog import (
        DEFAULT_APPS,
        HIMALAYA_SHIM_ROOT,
        HIMALAYA_SHIM_RUN,
        egress_for_slug,
        preferred_regions_for_slug,
        validate_spec,
    )

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "vivaldi-mail")
    assert raw["connection"] == "skill_bridge"
    assert raw.get("egress") == "overseas"
    assert egress_for_slug("vivaldi-mail") == "overseas"
    assert preferred_regions_for_slug("vivaldi-mail") == ["overseas"]
    spec = validate_spec("skill_bridge", raw["spec"])
    assert spec["manifest"]["name"] == "himalaya"
    assert spec["manifest"]["cli"]["command"] == [HIMALAYA_SHIM_RUN]
    assert spec["env_defaults"]["IMAP_URL"] == "imaps://imap.vivaldi.net:993"
    assert spec["env_defaults"]["SMTP_URL"] == "smtps://smtp.vivaldi.net:465"
    assert spec["env_defaults"]["SKILL_ROOT"] == HIMALAYA_SHIM_ROOT
    assert spec["env_defaults"]["STATE_DIR"].endswith("/vivaldi-mail")


def test_qqmail_mcp_delisted_from_default_apps():
    from portunus_gateway.catalog import DEFAULT_APPS
    from portunus_gateway.toml_catalog import load_apps_dir, packaged_apps_dir

    assert not any(a["slug"] == "qqmail" for a in DEFAULT_APPS)
    loaded = load_apps_dir(packaged_apps_dir())
    assert not any(r.slug == "qqmail" for r in loaded.recipes)


@pytest.mark.anyio
async def test_qqmail_ensure_defaults_delists_catalog():
    import uuid

    from portunus_gateway.catalog import CatalogStore
    from portunus_gateway.db.models import Base, CatalogApp, Server
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async with sf() as s:
        s.add(
            CatalogApp(
                id=uuid.uuid4(),
                slug="qqmail",
                label="QQ Mail",
                name="QQ Mail",
                description="stale oauth mcp",
                connection="oauth",
                popular=True,
                categories=["Productivity"],
                tags=["qqmail"],
                spec={"url": "https://api.mail.qq.com/mcp", "allow_bearer_token": True},
                sort_order=24,
            )
        )
        s.add(
            Server(
                id=uuid.uuid4(),
                name="QQ Mail",
                slug="qqmail",
                kind="remote",
                url="https://api.mail.qq.com/mcp",
                recipe_slug="qqmail",
                enabled=True,
                vault_version=2,
            )
        )
        await s.commit()

    store = CatalogStore(sf)
    await store.ensure_defaults()

    async with sf() as s:
        assert (
            await s.execute(select(CatalogApp).where(CatalogApp.slug == "qqmail"))
        ).scalar_one_or_none() is None
        srv = (
            await s.execute(select(Server).where(Server.slug == "qqmail"))
        ).scalar_one()
        assert srv.enabled is False
    await engine.dispose()


def test_validate_cubox_skill_bridge_spec():
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": CUBOX_MANIFEST,
            "secrets": [{"name": "CUBOX_TOKEN", "label": "Cubox API token"}],
            "env_defaults": {"CUBOX_SERVER": "cubox.pro"},
        },
    )
    assert out["manifest"]["backend"] == "json_cli"
    assert out["manifest"]["auth"]["type"] == "none"
    assert out["secrets"][0]["name"] == "CUBOX_TOKEN"
    assert out["env_defaults"]["CUBOX_SERVER"] == "cubox.pro"
    names = {t["name"] for t in out["manifest"]["tools"]}
    assert {"folder_list", "card_list", "card_rag", "save", "delete"} <= names
    folder = next(t for t in out["manifest"]["tools"] if t["name"] == "folder_list")
    assert folder["cli"] == ["folder", "list"]


def test_validate_proton_drive_auth_seed():
    from portunus_gateway.catalog import (
        DEFAULT_APPS,
        PROTON_DRIVE_MANIFEST,
        PROTON_DRIVE_SKILL_ROOT,
        PROTON_DRIVE_STATE_DIR,
        preferred_regions_for_slug,
        validate_spec,
    )

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "proton-drive")
    assert raw["connection"] == "skill_bridge"
    assert raw["egress"] == "overseas"
    assert preferred_regions_for_slug("proton-drive") == ["overseas"]
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": PROTON_DRIVE_MANIFEST,
            "secrets": [],
            "env_defaults": {
                "SKILL_ROOT": PROTON_DRIVE_SKILL_ROOT,
                "STATE_DIR": PROTON_DRIVE_STATE_DIR,
                "PROTON_DRIVE_BIN": "/usr/local/bin/proton-drive",
                "PROTON_DRIVE_CREDENTIALS_STORE": "unsafe_file",
            },
        },
    )
    assert out["manifest"]["auth"]["type"] == "oauth_link"
    assert out["manifest"]["auth"]["browser_capture"] is True
    assert out["manifest"]["auth"]["token_paste"] is False
    names = {t["name"] for t in out["manifest"]["tools"]}
    assert {
        "fs_list",
        "fs_upload",
        "fs_download",
        "sharing_status",
        "photo_timeline",
        "album_list",
        "album_photos",
    } <= names
    assert "login" not in names


def test_validate_agent_mail_auth_seed():
    from portunus_gateway.catalog import (
        AGENT_MAIL_MANIFEST,
        AGENT_MAIL_SKILL_ROOT,
        AGENT_MAIL_STATE_DIR,
        DEFAULT_APPS,
        preferred_regions_for_slug,
        validate_spec,
    )

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "agent-mail")
    assert raw["connection"] == "skill_bridge"
    assert raw["egress"] == "domestic"
    assert preferred_regions_for_slug("agent-mail") == ["domestic"]
    out = validate_spec(
        "skill_bridge",
        {
            "manifest": AGENT_MAIL_MANIFEST,
            "secrets": [],
            "env_defaults": {
                "SKILL_ROOT": AGENT_MAIL_SKILL_ROOT,
                "STATE_DIR": AGENT_MAIL_STATE_DIR,
            },
        },
    )
    assert out["manifest"]["auth"]["type"] == "oauth_link"
    assert out["manifest"]["auth"]["browser_capture"] is True
    assert out["manifest"]["auth"]["token_paste"] is False
    names = {t["name"] for t in out["manifest"]["tools"]}
    assert {
        "me",
        "message_list",
        "message_read",
        "message_search",
        "message_send",
        "attachment_upload",
    } <= names
    assert "login" not in names


def test_raindrop_http_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "raindrop")
    assert raw["connection"] == "http"
    spec = validate_spec("http", raw["spec"])
    assert spec["url"] == "https://api.raindrop.io/rest/v2/ai/mcp"
    assert "auth_header" not in spec


def test_youdaonote_http_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, preferred_regions_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "youdaonote")
    assert raw["connection"] == "http"
    spec = validate_spec("http", raw["spec"])
    assert spec["url"] == "https://open.mail.163.com/api/ynote/mcp/sse"
    assert spec["auth_header"] == "x-api-key"
    assert spec["token_prefix"] == ""
    assert preferred_regions_for_slug("youdaonote") == ["domestic"]


def test_validate_ima_skill_bridge_spec():
    from portunus_gateway.catalog import IMA_MANIFEST

    out = validate_spec(
        "skill_bridge",
        {
            "manifest": IMA_MANIFEST,
            "secrets": [
                {"name": "IMA_OPENAPI_CLIENTID", "label": "IMA Client ID"},
                {"name": "IMA_OPENAPI_APIKEY", "label": "IMA API Key"},
            ],
        },
    )
    assert out["manifest"]["http"]["dispatch_mode"] == "url_path"
    names = {s["name"] for s in out["secrets"]}
    assert names == {"IMA_OPENAPI_CLIENTID", "IMA_OPENAPI_APIKEY"}
    assert {t["name"] for t in out["manifest"]["tools"]} >= {"search_note", "import_urls"}


def test_validate_qqmusic_skill_bridge_spec():
    from portunus_gateway.catalog import QQMUSIC_MANIFEST

    out = validate_spec(
        "skill_bridge",
        {
            "manifest": QQMUSIC_MANIFEST,
            "secrets": [{"name": "QQMUSIC_API_KEY", "label": "QQ Music API key"}],
        },
    )
    assert out["manifest"]["http"]["dispatch_mode"] == "url_path"
    assert out["manifest"]["http"]["body_envelope"] == "params_comm"
    assert out["manifest"]["http"]["const"]["skill_version"] == "0.0.3"
    assert {s["name"] for s in out["secrets"]} == {"QQMUSIC_API_KEY"}
    assert {t["name"] for t in out["manifest"]["tools"]} >= {
        "discover_search",
        "charts",
        "me_report",
    }


def test_weknora_http_seed_url_from_operator():
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "weknora")
    assert raw["connection"] == "http"
    spec = validate_spec("http", raw["spec"])
    assert spec["url_from_operator"] is True
    assert "url" not in spec


def test_xunlei_http_seed_url_is_credential():
    from portunus_gateway.catalog import DEFAULT_APPS, egress_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "xunlei")
    assert raw["connection"] == "http"
    spec = validate_spec("http", raw["spec"])
    assert spec["url_from_operator"] is True
    assert spec["url_is_credential"] is True
    assert "auth_header" not in spec
    assert egress_for_slug("xunlei") == "domestic"


def test_merge_skill_bridge_seed_keeps_operator_fields():
    import copy

    from portunus_gateway.catalog import _merge_skill_bridge_seed

    seed = validate_spec(
        "skill_bridge",
        {
            "manifest": WEREAD_MANIFEST,
            "secrets": [{"name": "WEREAD_API_KEY", "label": "API key"}],
            "env_defaults": {"SKILL_ROOT": "/opt/weread"},
        },
    )
    live = copy.deepcopy(seed)
    live["secrets"] = [
        {"name": "WEREAD_API_KEY", "label": "Operator label", "secret": True},
        {"name": "EXTRA_TOKEN", "label": "extra", "secret": True},
    ]
    live["env_defaults"] = {"SKILL_ROOT": "/opt/custom", "OPERATOR_ONLY": "1"}
    live["command"] = "/usr/bin/python3"
    live["args"] = ["-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"]
    grown = copy.deepcopy(seed)
    grown["manifest"]["tools"] = [
        *grown["manifest"]["tools"],
        {"name": "extra_tool", "description": "added", "dispatch": "/extra", "input": {}},
    ]
    grown["env_defaults"] = {"SKILL_ROOT": "/opt/weread", "NEW_SEED": "yes"}
    grown["secrets"] = [
        *grown["secrets"],
        {"name": "NEW_SECRET", "label": "from seed", "secret": True},
    ]
    merged = validate_spec("skill_bridge", _merge_skill_bridge_seed(live, grown))
    assert "extra_tool" in {t["name"] for t in merged["manifest"]["tools"]}
    assert [s["name"] for s in merged["secrets"]] == [
        "WEREAD_API_KEY",
        "EXTRA_TOKEN",
        "NEW_SECRET",
    ]
    assert merged["secrets"][0]["label"] == "Operator label"
    assert merged["env_defaults"]["SKILL_ROOT"] == "/opt/custom"
    assert merged["env_defaults"]["OPERATOR_ONLY"] == "1"
    assert merged["env_defaults"]["NEW_SEED"] == "yes"
    assert merged["command"] == "/usr/bin/python3"
    assert merged["args"] == live["args"]


def test_thundermail_jmap_seed_shape():
    from portunus_gateway.catalog import DEFAULT_APPS, preferred_regions_for_slug, validate_spec

    raw = next(a for a in DEFAULT_APPS if a["slug"] == "thundermail")
    assert raw["connection"] == "http"
    spec = validate_spec("http", raw["spec"])
    assert spec["url"] == "portunus-jmap://thundermail"
    assert spec["auth_scheme"] == "basic"
    assert "client_id" not in spec
    assert preferred_regions_for_slug("thundermail") == ["overseas"]
