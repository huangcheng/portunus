import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from portunus_gateway.db.models import (
    AgentKey,
    AuditEvent,
    Base,
    Grant,
    OperatorSession,
    Server,
    Tool,
    VaultEntry,
)
from portunus_gateway.db.session import async_session, engine

pytestmark = pytest.mark.anyio

GATEWAY_DIR = Path(__file__).resolve().parent.parent


async def _create_all() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _server(slug: str = "demo", **kw) -> Server:
    return Server(name=kw.pop("name", slug), slug=slug, kind="stdio", command="npx",
                  args=["--ignore-scripts", "demo@1.0.0"], **kw)


async def test_server_tool_grant_roundtrip():
    await _create_all()
    async with async_session() as s:
        srv = Server(name="demo", slug="demo", kind="stdio", enabled=False,
                     command="npx", args=["--ignore-scripts", "demo@1.0.0"])
        s.add(srv)
        await s.flush()
        s.add(Tool(server_id=srv.id, upstream_name="ping", schema={}, schema_hash="h",
                   enabled=False))
        key = AgentKey(key_id="a" * 32, digest=b"\x00" * 32)
        s.add(key)
        await s.flush()
        s.add(Grant(key_id=key.key_id, server_id=srv.id))
        await s.commit()

    async with async_session() as s:
        srv = (await s.execute(select(Server))).scalar_one()
        assert isinstance(srv.id, uuid.UUID)
        assert srv.args == ["--ignore-scripts", "demo@1.0.0"]
        assert srv.enabled is False
        tool = (await s.execute(select(Tool))).scalar_one()
        assert tool.server_id == srv.id and tool.schema == {} and tool.enabled is False  # explicit
        key = (await s.execute(select(AgentKey))).scalar_one()
        assert key.digest == b"\x00" * 32 and key.generation == 0 and key.revoked_at is None
        grant = (await s.execute(select(Grant))).scalar_one()
        assert (grant.key_id, grant.server_id) == (key.key_id, srv.id)


async def test_defaults_server_off_tools_on():
    await _create_all()
    async with async_session() as s:
        srv = _server()
        s.add(srv)
        await s.flush()
        s.add(Tool(server_id=srv.id, upstream_name="t", schema={}, schema_hash="h"))
        await s.commit()
        assert srv.enabled is False
        assert srv.vault_version == 0
        assert (await s.execute(select(Tool))).scalar_one().enabled is True


async def test_server_slug_unique_casefold():
    await _create_all()
    async with async_session() as s:
        s.add(_server("Demo"))
        await s.commit()
        assert (await s.execute(select(Server.slug))).scalar_one() == "demo"
        s.add(_server("DEMO", name="other"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_slug_casefold_enforced_at_db_level():
    from sqlalchemy import insert

    await _create_all()
    async with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(insert(Server.__table__).values(
                id=uuid.uuid4(), name="x", slug="UPPER", kind="stdio", enabled=False,
                vault_version=0))


async def test_agent_key_id_unique():
    await _create_all()
    async with async_session() as s:
        s.add(AgentKey(key_id="b" * 32, digest=b"\x01" * 32))
        await s.commit()
        s.add(AgentKey(key_id="b" * 32, digest=b"\x02" * 32))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_grant_unique_per_key_server():
    await _create_all()
    async with async_session() as s:
        srv, key = _server(), AgentKey(key_id="c" * 32, digest=b"\x00" * 32)
        s.add_all([srv, key])
        await s.flush()
        s.add(Grant(key_id=key.key_id, server_id=srv.id))
        await s.commit()
        s.add(Grant(key_id=key.key_id, server_id=srv.id))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_tool_unique_per_server_upstream_name():
    await _create_all()
    async with async_session() as s:
        srv = _server()
        s.add(srv)
        await s.flush()
        s.add(Tool(server_id=srv.id, upstream_name="t", schema={}, schema_hash="h"))
        await s.commit()
        s.add(Tool(server_id=srv.id, upstream_name="t", schema={}, schema_hash="h2"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_foreign_keys_enforced():
    await _create_all()
    async with async_session() as s:
        s.add(Grant(key_id="d" * 32, server_id=uuid.uuid4()))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_server_delete_cascades():
    await _create_all()
    async with async_session() as s:
        srv, key = _server(), AgentKey(key_id="e" * 32, digest=b"\x00" * 32)
        s.add_all([srv, key])
        await s.flush()
        s.add_all([
            Tool(server_id=srv.id, upstream_name="t", schema={}, schema_hash="h"),
            VaultEntry(server_id=srv.id, name="API_KEY", ciphertext=b"x" * 40, key_version=1),
            Grant(key_id=key.key_id, server_id=srv.id),
        ])
        await s.commit()
        await s.delete(srv)
        await s.commit()
        for model in (Tool, VaultEntry, Grant):
            assert (await s.execute(select(model))).first() is None


async def test_vault_entry_unique_name_per_server():
    await _create_all()
    async with async_session() as s:
        srv = _server()
        s.add(srv)
        await s.flush()
        s.add(VaultEntry(server_id=srv.id, name="K", ciphertext=b"x", key_version=1))
        await s.commit()
        s.add(VaultEntry(server_id=srv.id, name="K", ciphertext=b"y", key_version=1))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_server_kind_checked():
    await _create_all()
    async with async_session() as s:
        s.add(Server(name="bad", slug="bad", kind="docker"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_session_and_audit_rows():
    await _create_all()
    async with async_session() as s:
        s.add(OperatorSession(token_hash=b"\x01" * 32, csrf_token="csrf"))
        s.add(AuditEvent(actor="operator", action="server.enable", target="demo",
                         detail={"ip": "127.0.0.1"}))
        await s.commit()
    async with async_session() as s:
        sess = (await s.execute(select(OperatorSession))).scalar_one()
        assert sess.created_at is not None and sess.expires_at is not None
        ev = (await s.execute(select(AuditEvent))).scalar_one()
        assert ev.detail == {"ip": "127.0.0.1"} and ev.created_at is not None


def test_alembic_migration_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path}/mig.db"
    cfg = Config(str(GATEWAY_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(GATEWAY_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == []
    finally:
        eng.dispose()
    command.downgrade(cfg, "base")


def test_alembic_upgrade_async_url_creates_data_dir(tmp_path):
    """Production path: aiosqlite URL (``_run_async``) with a not-yet-existing parent dir."""
    db = tmp_path / "does" / "not" / "exist" / "portunus.db"
    assert not db.parent.exists()
    url = f"sqlite+aiosqlite:///{db}"
    cfg = Config(str(GATEWAY_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(GATEWAY_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    assert db.exists()
    eng = create_engine(f"sqlite:///{db}")
    try:
        with eng.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            assert {"servers", "tools", "vault_entries", "agent_keys", "grants",
                    "operator_sessions", "audit_events", "alembic_version"} <= tables
            assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    finally:
        eng.dispose()


def test_alembic_upgrade_default_url_in_fresh_cwd(tmp_path, monkeypatch):
    """Fresh checkout: no URL override, default ``./data/portunus.db`` relative to cwd."""
    monkeypatch.delenv("PORTUNUS_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = Config(str(GATEWAY_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(GATEWAY_DIR / "alembic"))
    command.upgrade(cfg, "head")
    assert (tmp_path / "data" / "portunus.db").exists()
