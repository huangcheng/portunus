"""Server ``preferred_regions`` soft affinity: API, probe, start and invoke wiring (fleet Task 4)."""

from __future__ import annotations

import logging
import os
import stat
import uuid

import httpx
import pytest
from portunus_gateway.db.models import AuditEvent, Server
from portunus_gateway.db.session import async_session
from portunus_gateway.omni import Omni
from portunus_gateway.operator_api import create_app, require_operator, require_step_up
from portunus_gateway.registry import Registry
from portunus_gateway.runner_client import RunnerRequestError
from portunus_gateway.runner_pool import RunnerPool
from portunus_gateway.slots import SlotManager
from portunus_gateway.vault import Vault
from sqlalchemy import select

pytestmark = pytest.mark.anyio

NPX_ARGS = ["--ignore-scripts", "demo@1.0.0"]


class FakeSite:
    """Runner stand-in that advertises a site/regions and records what it was sent."""

    def __init__(self, site_id: str, regions: tuple[str, ...], rpc_error=None) -> None:
        self.site_id = site_id
        self.regions = list(regions)
        self.connected = True
        self.conn_seq = 1
        self.calls: list[tuple[str, dict]] = []
        self.rpc_error = rpc_error
        self.on_connected = None
        self.on_child_exited = None

    @property
    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    async def request(self, op, *, timeout, conn_seq=None, **fields):
        if not self.connected:
            raise RunnerRequestError("runner_unavailable", "not connected", sent=False)
        self.calls.append((op, fields))
        if op == "probe":
            return {"tools": [{"name": "alpha", "inputSchema": {"type": "object"}}]}
        if op == "start":
            return {"child_id": fields["child_id"], "pid": 1}
        if op == "rpc":
            if self.rpc_error is not None:
                raise self.rpc_error
            return {"result": {"content": [{"type": "text", "text": self.site_id}]}}
        if op == "kill":
            return {}
        raise AssertionError(op)


@pytest.fixture
def site_a():
    return FakeSite("site-a", ("region-a",))


@pytest.fixture
def site_b():
    return FakeSite("site-b", ("region-b",))


@pytest.fixture
def pool(site_a, site_b):
    return RunnerPool([site_a, site_b])


@pytest.fixture
def trusted_bin(tmp_path):
    """A trusted dir holding an inert ``npx`` stub (the fake sites never exec anything)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text("#!/bin/sh\nexit 0\n")
    npx.chmod(npx.stat().st_mode | stat.S_IXUSR)
    return bin_dir


@pytest.fixture
async def registry(db, pool, trusted_bin):
    return Registry(
        async_session,
        Vault({1: os.urandom(32)}),
        pool,
        trusted_path=[str(trusted_bin), "/usr/bin", "/bin"],
        binary_allowlist=[],
    )


@pytest.fixture
async def http(registry):
    app = create_app(registry)
    app.dependency_overrides[require_operator] = lambda: "test-operator"
    app.dependency_overrides[require_step_up] = lambda: "test-operator"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _body(slug="pref", **over):
    body = {
        "name": slug,
        "slug": slug,
        "kind": "stdio",
        "command": "npx",
        "args": list(NPX_ARGS),
        "env": {},
    }
    body.update(over)
    return body


async def _mk(http, **over) -> str:
    r = await http.post("/api/servers", json=_body(**over))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _audit(action: str) -> list[AuditEvent]:
    async with async_session() as s:
        rows = (await s.execute(select(AuditEvent).where(AuditEvent.action == action))).scalars()
        return list(rows)


# ---- API ------------------------------------------------------------------------------


async def test_create_defaults_to_no_preference(http):
    r = await http.post("/api/servers", json=_body())
    assert r.status_code == 201
    assert r.json()["preferred_regions"] == []
    assert (await http.get("/api/servers")).json()[0]["preferred_regions"] == []


async def test_create_accepts_preferred_regions(http):
    r = await http.post("/api/servers", json=_body(preferred_regions=["region-a", "region-b"]))
    assert r.status_code == 201
    assert r.json()["preferred_regions"] == ["region-a", "region-b"]
    async with async_session() as s:
        row = (await s.execute(select(Server))).scalar_one()
    assert row.preferred_regions == ["region-a", "region-b"]


async def test_create_dedupes_preserving_order(http):
    r = await http.post(
        "/api/servers", json=_body(preferred_regions=["region-b", "region-a", "region-b"])
    )
    assert r.json()["preferred_regions"] == ["region-b", "region-a"]


@pytest.mark.parametrize(
    "bad",
    [
        "region-a",  # not a list
        [""],
        [" region-a"],
        ["region a"],
        ["a" * 80],
        [f"r{i}" for i in range(17)],
        [1],
    ],
)
async def test_create_rejects_bad_regions(http, bad):
    r = await http.post("/api/servers", json=_body(preferred_regions=bad))
    assert r.status_code == 422


async def test_update_sets_and_clears_preference(http):
    sid = await _mk(http)
    r = await http.patch(f"/api/servers/{sid}", json={"preferred_regions": ["region-a"]})
    assert r.status_code == 200
    assert r.json()["preferred_regions"] == ["region-a"]
    r = await http.patch(f"/api/servers/{sid}", json={"preferred_regions": []})
    assert r.json()["preferred_regions"] == []
    events = await _audit("server.update")
    assert [e.detail for e in events] == [
        {"preferred_regions": ["region-a"]},
        {"preferred_regions": []},
    ]


async def test_update_unknown_server_404_and_unknown_field_422(http):
    r = await http.patch(f"/api/servers/{uuid.uuid4()}", json={"preferred_regions": []})
    assert r.status_code == 404
    sid = await _mk(http)
    r = await http.patch(f"/api/servers/{sid}", json={"not_a_field": True})
    assert r.status_code == 422


async def test_update_custom_name_slug_and_argv(http, db):
    sid = await _mk(http, slug="edit-me")
    async with async_session() as s:
        before_vv = (
            await s.execute(select(Server).where(Server.id == uuid.UUID(sid)))
        ).scalar_one().vault_version or 0
    r = await http.patch(
        f"/api/servers/{sid}",
        json={
            "name": "Renamed",
            "slug": "renamed-mcp",
            "command": "npx",
            "args": ["-y", "--ignore-scripts", "demo@2.0.0"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Renamed"
    assert body["slug"] == "renamed-mcp"
    assert body["args"] == ["-y", "--ignore-scripts", "demo@2.0.0"]
    assert body["recipe_slug"] is None
    assert body["vault_version"] == int(before_vv) + 1


async def test_update_slug_conflict_409(http):
    await _mk(http, slug="taken")
    sid = await _mk(http, slug="other")
    r = await http.patch(f"/api/servers/{sid}", json={"slug": "taken"})
    assert r.status_code == 409
    assert r.json()["code"] == "conflict"


async def test_update_rejects_bad_npx_argv(http):
    sid = await _mk(http)
    r = await http.patch(
        f"/api/servers/{sid}",
        json={"args": ["@z_ai/mcp-server@latest"]},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "spawn_rejected"


async def test_update_catalog_server_rejects_slug_and_argv(http, db):
    """Catalog Connect freezes slug/command; PATCH must not rewrite the recipe binding."""
    sid = await _mk(http, slug="catalogish")
    async with async_session() as s:
        server = (
            await s.execute(select(Server).where(Server.id == uuid.UUID(sid)))
        ).scalar_one()
        server.recipe_slug = "catalogish"
        await s.commit()
    bad_slug = await http.patch(f"/api/servers/{sid}", json={"slug": "nope"})
    assert bad_slug.status_code == 422
    bad_argv = await http.patch(
        f"/api/servers/{sid}",
        json={"args": ["-y", "--ignore-scripts", "demo@9.9.9"]},
    )
    assert bad_argv.status_code == 422
    # Name remains editable.
    ok = await http.patch(f"/api/servers/{sid}", json={"name": "Still catalog"})
    assert ok.status_code == 200
    assert ok.json()["name"] == "Still catalog"
    assert ok.json()["slug"] == "catalogish"


async def test_create_with_recipe_slug_binds(http):
    r = await http.post(
        "/api/servers",
        json={
            "name": "Raindrop",
            "slug": "raindrop",
            "kind": "remote",
            "url": "https://api.raindrop.io/rest/v2/ai/mcp",
            "recipe_slug": "raindrop",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["recipe_slug"] == "raindrop"
    # Custom create must not auto-bind just because the slug looks catalog-like.
    custom = await http.post(
        "/api/servers",
        json={
            "name": "Custom raindrop-ish",
            "slug": "raindrop-custom",
            "kind": "remote",
            "url": "https://example.com/mcp",
        },
    )
    assert custom.status_code == 201, custom.text
    assert custom.json()["recipe_slug"] is None


async def test_url_from_operator_recipe_allows_url_patch(http, db):
    r = await http.post(
        "/api/servers",
        json={
            "name": "WeKnora",
            "slug": "weknora",
            "kind": "remote",
            "url": "https://example.com/mcp/old",
            "recipe_slug": "weknora",
        },
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["recipe_slug"] == "weknora"
    patched = await http.patch(
        f"/api/servers/{sid}",
        json={"url": "https://example.com/mcp/new"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["url"] == "https://example.com/mcp/new"


# ---- probe ----------------------------------------------------------------------------


async def test_probe_prefers_region_a(http, site_a, site_b):
    sid = await _mk(http, preferred_regions=["region-a"])
    assert (await http.post(f"/api/servers/{sid}/probe")).status_code == 200
    assert site_a.ops == ["probe"] and site_b.ops == []
    (ev,) = await _audit("server.probe")
    assert ev.detail["site_id"] == "site-a" and ev.detail["preferred_miss"] is False


async def test_probe_falls_back_to_b_with_preferred_miss(http, site_a, site_b):
    site_a.connected = False
    sid = await _mk(http, preferred_regions=["region-a"])
    assert (await http.post(f"/api/servers/{sid}/probe")).status_code == 200
    assert site_a.ops == [] and site_b.ops == ["probe"]
    (ev,) = await _audit("server.probe")
    assert ev.detail["site_id"] == "site-b" and ev.detail["preferred_miss"] is True


async def test_probe_without_preference_is_unchanged(http, site_a, site_b):
    sid = await _mk(http)
    assert (await http.post(f"/api/servers/{sid}/probe")).status_code == 200
    # least-loaded tie-break: lowest site id; never a miss without a preference
    assert site_a.ops == ["probe"] and site_b.ops == []
    (ev,) = await _audit("server.probe")
    assert ev.detail["preferred_miss"] is False


async def test_probe_audit_carries_no_env_or_argv(http):
    sid = await _mk(http, env={"TOKEN_VALUE": "s3cret-literal"}, preferred_regions=["region-a"])
    await http.post(f"/api/servers/{sid}/probe")
    (ev,) = await _audit("server.probe")
    assert "s3cret-literal" not in str(ev.detail) and "TOKEN_VALUE" not in str(ev.detail)


# ---- start / invoke -------------------------------------------------------------------


async def test_start_payload_carries_server_preference(http, registry):
    sid = await _mk(http, preferred_regions=["region-b"])
    start = await registry.start_payload(uuid.UUID(sid))
    assert start["preferred_regions"] == ["region-b"]
    other = await _mk(http, slug="plain")
    assert (await registry.start_payload(uuid.UUID(other)))["preferred_regions"] == []


async def _invoke(http, registry, pool, authz, sid: str, slug_rpc_error=None):
    manager = SlotManager(pool, authz, start_ack_s=0.3, stop_retry_s=0.05)
    manager.attach(pool)
    omni = Omni(
        session_factory=async_session, registry=registry, slots=manager, runner=pool, authz=authz
    )
    server_id = uuid.UUID(sid)
    authz._grants.add(("k1", server_id))
    authz.bump()
    async with async_session() as s:
        server = await s.get(Server, server_id)
    from types import SimpleNamespace

    tool = SimpleNamespace(upstream_name="alpha")
    return manager, await omni._run_invoke("k1", server, tool, {}, 1.0)  # noqa: SLF001


async def test_invoke_prefers_region_a(http, registry, pool, authz, site_a, site_b):
    sid = await _mk(http, preferred_regions=["region-a"])
    manager, result = await _invoke(http, registry, pool, authz, sid)
    (slot,) = manager.slots.values()
    assert slot.site_id == "site-a" and slot.preferred_miss is False
    assert site_a.ops == ["start", "rpc"] and site_b.ops == []
    assert result.content[0].text == "site-a"


async def test_invoke_only_b_up_schedules_b_with_preferred_miss(
    http, registry, pool, authz, site_a, site_b, caplog
):
    site_a.connected = False
    sid = await _mk(http, preferred_regions=["region-a"])
    with caplog.at_level(logging.INFO, logger="portunus.gateway.slots"):
        manager, result = await _invoke(http, registry, pool, authz, sid)
    (slot,) = manager.slots.values()
    assert slot.site_id == "site-b" and slot.preferred_miss is True
    assert site_b.ops == ["start", "rpc"] and result.content[0].text == "site-b"
    miss = [r for r in caplog.records if "preferred_miss=True" in r.getMessage()]
    assert len(miss) == 1
    msg = miss[0].getMessage()
    assert "site-b" in msg and sid in msg
    # ids only: no argv / env in the log line
    assert "demo@1.0.0" not in msg and "npx" not in msg


async def test_invoke_empty_preference_unchanged(http, registry, pool, authz, site_a, site_b, caplog):
    sid = await _mk(http)
    with caplog.at_level(logging.INFO, logger="portunus.gateway.slots"):
        manager, _ = await _invoke(http, registry, pool, authz, sid)
    (slot,) = manager.slots.values()
    assert slot.site_id == "site-a" and slot.preferred_miss is False
    assert not [r for r in caplog.records if "preferred_miss=True" in r.getMessage()]


async def test_invoke_on_fallback_site_surfaces_real_child_error(
    http, registry, pool, authz, site_a, site_b
):
    from portunus_shared import PortunusError

    site_a.connected = False
    site_b.rpc_error = RunnerRequestError("upstream_crash", "child exited", sent=True)
    sid = await _mk(http, preferred_regions=["region-a"])
    with pytest.raises(PortunusError) as ei:
        await _invoke(http, registry, pool, authz, sid)
    assert ei.value.code == "upstream_crash"  # not masked as runner_unavailable
