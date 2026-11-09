import asyncio
import uuid

import pytest
from sqlalchemy import select

from portunus_gateway import authz as authz_mod
from portunus_gateway.authz import AuthzStore
from portunus_gateway.db.models import Grant, Server
from portunus_gateway.db.session import async_session
from portunus_gateway.keys import AuthError, mint_agent_key, revoke_key, verify_agent_token

pytestmark = pytest.mark.anyio


async def _server(slug="demo") -> uuid.UUID:
    async with async_session() as s:
        srv = Server(name=slug, slug=slug, kind="stdio", command="npx", args=["x@1.0.0"])
        s.add(srv)
        await s.commit()
        return srv.id


async def _grant(key_id: str, server_id: uuid.UUID) -> None:
    async with async_session() as s:
        s.add(Grant(key_id=key_id, server_id=server_id))
        await s.commit()


def test_fresh_store_is_empty():
    st = AuthzStore()
    assert st.generation == 0
    assert not st.is_revoked("k") and not st.has_grant("k", uuid.uuid4())


def test_bump_increments_generation():
    st = AuthzStore()
    assert st.bump() == 1 and st.bump() == 2 and st.generation == 2


async def test_reload_from_db_loads_grants_and_revocations(pepper_file, db):
    _, a = await mint_agent_key(expires_at=None)
    _, b = await mint_agent_key(expires_at=None)
    sid, other = await _server("a"), await _server("b")
    await _grant(a.key_id, sid)
    await _grant(b.key_id, other)
    st = AuthzStore()
    await st.reload_from_db()
    assert st.has_grant(a.key_id, sid) and not st.has_grant(a.key_id, other)
    assert st.has_grant(b.key_id, other)
    assert not st.is_revoked(a.key_id)


async def test_reload_picks_up_changes(pepper_file, db):
    _, row = await mint_agent_key(expires_at=None)
    sid = await _server()
    st = AuthzStore()
    await st.reload_from_db()
    assert not st.has_grant(row.key_id, sid)
    await _grant(row.key_id, sid)
    await st.reload_from_db()
    assert st.has_grant(row.key_id, sid)
    async with async_session() as s:
        g = (await s.execute(select(Grant))).scalar_one()
        await s.delete(g)
        await s.commit()
    await st.reload_from_db()
    assert not st.has_grant(row.key_id, sid)


async def test_reload_bumps_generation_when_state_changes(pepper_file, db):
    _, row = await mint_agent_key(expires_at=None)
    st = AuthzStore()
    await st.reload_from_db()
    g0 = st.generation
    await st.reload_from_db()  # nothing changed
    assert st.generation == g0
    await _grant(row.key_id, await _server())
    await st.reload_from_db()
    assert st.generation > g0


async def test_revoke_bumps_generation_has_grant_readable_verify_fails(pepper_file, authz):
    token, row = await mint_agent_key(expires_at=None)
    sid = await _server()
    await _grant(row.key_id, sid)
    await authz.reload_from_db()
    assert not authz.is_revoked(row.key_id) and authz.has_grant(row.key_id, sid)
    before = authz.generation

    await revoke_key(row.key_id)

    assert authz.generation > before
    assert authz.is_revoked(row.key_id)
    # Grants stay readable (callers must consult is_revoked / verify); they are not silently dropped.
    assert authz.has_grant(row.key_id, sid)
    with pytest.raises(AuthError) as e:
        await verify_agent_token(token)
    assert e.value.code == "revoked"


async def test_revoked_state_survives_reload(pepper_file, authz):
    _, row = await mint_agent_key(expires_at=None)
    await revoke_key(row.key_id)
    fresh = AuthzStore()
    await fresh.reload_from_db()
    assert fresh.is_revoked(row.key_id)


async def test_reload_never_unrevokes_in_memory_state(pepper_file, db):
    """A reload that read the DB before a revoke committed must not clear the revocation."""
    _, row = await mint_agent_key(expires_at=None)
    st = AuthzStore()
    st.mark_revoked(row.key_id)  # DB still says not revoked (stale snapshot)
    await st.reload_from_db()
    assert st.is_revoked(row.key_id)


async def test_revoke_idempotent_does_not_bump_again(pepper_file, authz):
    _, row = await mint_agent_key(expires_at=None)
    await revoke_key(row.key_id)
    g = authz.generation
    await revoke_key(row.key_id)
    assert authz.generation == g


async def test_mark_revoked_bumps_once(pepper_file, db):
    st = AuthzStore()
    st.mark_revoked("k")
    st.mark_revoked("k")
    assert st.generation == 1 and st.is_revoked("k")


async def test_concurrent_reloads_do_not_apply_stale_snapshot_over_ungrant(pepper_file, db):
    """A reload that read the DB before an ungrant committed must not clobber the post-ungrant reload."""
    _, row = await mint_agent_key(expires_at=None)
    sid = await _server()
    await _grant(row.key_id, sid)
    st = AuthzStore()

    real_read = st._read_snapshot
    release_first = asyncio.Event()
    first_read_done = asyncio.Event()
    calls = 0

    async def gated_read():
        nonlocal calls
        calls += 1
        snap = await real_read()
        if calls == 1:  # first reload: read (grant present), then stall before swapping
            first_read_done.set()
            await release_first.wait()
        return snap

    st._read_snapshot = gated_read  # type: ignore[method-assign]

    stale = asyncio.create_task(st.reload_from_db())
    await first_read_done.wait()
    # Ungrant commits while the stale reload is in flight, then its own reload starts.
    async with async_session() as s:
        g = (await s.execute(select(Grant))).scalar_one()
        await s.delete(g)
        await s.commit()
    fresh = asyncio.create_task(st.reload_from_db())
    # Without serialization the fresh reload completes here while the stale one is still parked;
    # with the lock it stays blocked, so give it a bounded window and then let the stale one go.
    await asyncio.wait({fresh}, timeout=0.3)
    release_first.set()
    await asyncio.gather(stale, fresh)
    assert not st.has_grant(row.key_id, sid)


async def test_interleaved_grant_and_ungrant_reloads_converge(pepper_file, db):
    _, row = await mint_agent_key(expires_at=None)
    keep, drop = await _server("keep"), await _server("drop")
    await _grant(row.key_id, drop)
    st = AuthzStore()
    await st.reload_from_db()

    async def grant_keep():
        await _grant(row.key_id, keep)
        await st.reload_from_db()

    async def ungrant_drop():
        async with async_session() as s:
            await s.delete((await s.execute(select(Grant).where(Grant.server_id == drop))).scalar_one())
            await s.commit()
        await st.reload_from_db()

    await asyncio.gather(grant_keep(), ungrant_drop(), st.reload_from_db(), st.reload_from_db())
    assert st.has_grant(row.key_id, keep) and not st.has_grant(row.key_id, drop)


def test_module_singleton_exists():
    assert isinstance(authz_mod.authz_store, AuthzStore)
