"""Registry API: create stdio server, probe via the runner, enable tools (Task 9).

Two harnesses: the real runner in dev mode driving a fixture MCP server over stdio
(``runner_with_fixture``), and a scripted fake runner for failure / concurrency cases.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
from portunus_gateway.db.models import AuditEvent, Server, VaultEntry
from portunus_gateway.db.session import async_session
from portunus_gateway.operator_api import create_app, require_operator, require_step_up
from portunus_gateway.registry import Registry
from portunus_gateway.runner_client import RunnerClient, RunnerRequestError
from portunus_gateway.vault import Vault
from portunus_runner.main import RunnerServer
from sqlalchemy import select

pytestmark = pytest.mark.anyio

TOKEN = "tok-registry"
FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
FIXTURE_ARGS = ["--ignore-scripts", "fixture@1.0.0"]


def _vault() -> Vault:
    return Vault({1: os.urandom(32)})


def _app(registry: Registry, *, authed: bool = True):
    app = create_app(registry)
    if authed:
        app.dependency_overrides[require_operator] = lambda: "test-operator"
        app.dependency_overrides[require_step_up] = lambda: "test-operator"
    return app


@contextlib.asynccontextmanager
async def _http(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---- real runner + fixture ------------------------------------------------------------


@pytest.fixture
def policy_dir():
    """tmp dir: ``bin/npx`` stub that execs the fixture MCP server, plus a policy file."""
    d = Path(tempfile.mkdtemp(prefix="prg-", dir="/tmp"))
    (d / "bin").mkdir()
    npx = d / "bin" / "npx"
    npx.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}"\n')
    npx.chmod(npx.stat().st_mode | stat.S_IXUSR)
    policy = d / "policy.json"
    policy.write_text(
        json.dumps({"trusted_path": [str(d / "bin"), "/usr/bin", "/bin"], "binary_allowlist": []})
    )
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def runner_with_fixture(policy_dir):
    sock = str(policy_dir / "r.sock")
    srv = RunnerServer(
        socket_path=sock, token=TOKEN, dev=True, policy_path=str(policy_dir / "policy.json")
    )
    await srv.start()
    client = RunnerClient(sock, TOKEN, retry_initial_s=0.02, retry_max_s=0.05)
    client.start()
    await client.wait_connected(5)
    yield client, srv
    await client.close()
    await srv.close()


@pytest.fixture
async def real(runner_with_fixture, policy_dir, db):
    client, srv = runner_with_fixture
    registry = Registry(
        async_session,
        _vault(),
        client,
        trusted_path=[str(policy_dir / "bin"), "/usr/bin", "/bin"],
        binary_allowlist=[],
        probe_timeout_s=10,
    )
    async with _http(_app(registry)) as http:
        yield http, registry, srv


def _body(slug="fix", **over):
    body = {
        "name": slug,
        "slug": slug,
        "kind": "stdio",
        "command": "npx",
        "args": list(FIXTURE_ARGS),
        "env": {},
    }
    body.update(over)
    return body


async def test_probe_lists_tools_disabled(real):
    client, _registry, srv = real
    r = await client.post("/api/servers", json=_body())
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["enabled"] is False

    pr = await client.post(f"/api/servers/{sid}/probe")
    assert pr.status_code == 200, pr.text
    tools = (await client.get(f"/api/servers/{sid}/tools")).json()
    assert {t["name"] for t in tools} == {"alpha", "beta"}
    assert tools and all(t["enabled"] is False for t in tools)
    assert all(t["schema_hash"] and isinstance(t["schema"], dict) for t in tools)
    assert pr.json()["added"] == 2
    # the probe child is gone and never became an invoke child
    assert srv.children.empty()


async def test_env_literals_go_through_vault_to_probe_child(real):
    client, _registry, _srv = real
    secret = "s3cr3t-value-xyz"
    r = await client.post(
        "/api/servers",
        json=_body(env={"MY_SECRET": secret, "FAKE_MCP_ECHO_ENV": "MY_SECRET"}),
    )
    assert r.status_code == 201
    assert secret not in r.text
    assert sorted(r.json()["env_names"]) == ["FAKE_MCP_ECHO_ENV", "MY_SECRET"]
    assert r.json()["vault_version"] >= 1
    sid = r.json()["id"]

    async with async_session() as s:
        rows = (await s.execute(select(VaultEntry))).scalars().all()
        assert len(rows) == 2
        assert all(secret.encode() not in bytes(v.ciphertext) for v in rows)

    pr = await client.post(f"/api/servers/{sid}/probe")
    assert pr.status_code == 200, pr.text
    tools = {t["name"]: t for t in (await client.get(f"/api/servers/{sid}/tools")).json()}
    assert tools["echo_env"]["description"] == secret  # decrypted literal reached the child

    listing = await client.get("/api/servers")
    assert secret not in listing.text  # (the fixture echoes it into a *tool* description only)
    async with async_session() as s:
        evs = (await s.execute(select(AuditEvent))).scalars().all()
    assert secret not in json.dumps([e.detail for e in evs])


async def test_probe_timeout_kills_child(real):
    client, registry, srv = real
    registry.probe_timeout_s = 1
    r = await client.post(
        "/api/servers", json=_body(slug="hang", env={"FAKE_MCP_HANG": "1"})
    )
    pr = await client.post(f"/api/servers/{r.json()['id']}/probe")
    assert pr.status_code == 504
    assert pr.json()["code"] == "upstream_timeout"
    assert srv.children.empty()
    assert (await client.get(f"/api/servers/{r.json()['id']}/tools")).json() == []


async def test_paginated_tools_list(real):
    client, _registry, _srv = real
    r = await client.post(
        "/api/servers", json=_body(slug="pg", env={"FAKE_MCP_PAGES": "1"})
    )
    pr = await client.post(f"/api/servers/{r.json()['id']}/probe")
    assert pr.status_code == 200, pr.text
    names = {t["name"] for t in (await client.get(f"/api/servers/{r.json()['id']}/tools")).json()}
    assert names == {"alpha", "beta"}


# ---- create validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command,args",
    [
        ("npx", ["--ignore-scripts", "fixture@latest"]),
        ("npx", ["fixture@1.0.0"]),
        ("npx", ["--ignore-scripts", "fixture@1.0.0", "--eval"]),
        ("sh", ["-c", "id"]),
        ("/bin/sh", []),  # absolute but not allowlisted
    ],
)
async def test_create_rejects_bad_argv(real, command, args):
    client, _registry, _srv = real
    r = await client.post("/api/servers", json=_body(command=command, args=args))
    assert r.status_code == 422
    assert r.json()["code"] == "spawn_rejected"
    async with async_session() as s:
        assert (await s.execute(select(Server))).scalars().all() == []


@pytest.mark.parametrize(
    "env",
    [{"PORTUNUS_TOKEN": "x"}, {"LD_PRELOAD": "x"}, {"1BAD": "x"}, {"A B": "x"}, {"OK": 5}],
)
async def test_create_rejects_bad_env(real, env):
    client, _registry, _srv = real
    r = await client.post("/api/servers", json=_body(env=env))
    assert r.status_code == 422
    async with async_session() as s:
        assert (await s.execute(select(Server))).scalars().all() == []


async def test_create_allows_skill_bridge_manifest_env(real):
    """PORTUNUS_MANIFEST is the sole PORTUNUS_* exception (skill-bridge carrier)."""
    from portunus_shared import SKILL_BRIDGE_MANIFEST_ENV

    client, _registry, _srv = real
    r = await client.post(
        "/api/servers",
        json=_body(env={SKILL_BRIDGE_MANIFEST_ENV: '{"name":"weread","backend":"http_rpc"}'}),
    )
    assert r.status_code == 201, r.text
    assert SKILL_BRIDGE_MANIFEST_ENV in r.json()["env_names"]


async def test_create_duplicate_slug_conflicts_and_slug_casefolds(real):
    client, _registry, _srv = real
    assert (await client.post("/api/servers", json=_body(slug="dup"))).status_code == 201
    r = await client.post("/api/servers", json=_body(slug="DUP"))
    assert r.status_code == 409


async def test_create_remote_server(real):
    client, _registry, _srv = real
    r = await client.post(
        "/api/servers",
        json={
            "name": "Remote",
            "slug": "remote-demo",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
            "env": {"Authorization": "Bearer paste-me"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "remote" and body["url"] == "https://mcp.example.com/mcp"
    assert body["has_auth"] is True and body["enabled"] is False
    assert "Authorization" in body["env_names"]


async def test_create_bad_slug(real):
    client, _registry, _srv = real
    r = await client.post("/api/servers", json=_body(slug="has space"))
    assert r.status_code == 422


async def test_patch_stdio_env_upserts_vault(real):
    client, _registry, _srv = real
    created = await client.post(
        "/api/servers",
        json=_body(slug="nutstore", env={"WEBDAV_ROOT_URL": "https://dav.example/dav/"}),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["env_names"] == ["WEBDAV_ROOT_URL"]

    patched = await client.patch(
        f"/api/servers/{sid}",
        json={
            "env": {
                "WEBDAV_USERNAME": "me@example.com",
                "WEBDAV_PASSWORD": "app-pass",
                "WEBDAV_AUTH_ENABLED": "true",
            }
        },
    )
    assert patched.status_code == 200, patched.text
    names = set(patched.json()["env_names"])
    assert names == {
        "WEBDAV_ROOT_URL",
        "WEBDAV_USERNAME",
        "WEBDAV_PASSWORD",
        "WEBDAV_AUTH_ENABLED",
    }

    # remote accepts request headers via env; rejects invalid header names
    remote = await client.post(
        "/api/servers",
        json={
            "name": "r",
            "slug": "remote-no-env",
            "kind": "remote",
            "url": "https://mcp.example.com/mcp",
        },
    )
    rid = remote.json()["id"]
    ok = await client.patch(f"/api/servers/{rid}", json={"env": {"WyHeader": "mcp_token=abc"}})
    assert ok.status_code == 200, ok.text
    assert "WyHeader" in ok.json()["env_names"]
    assert ok.json()["has_auth"] is True
    bad = await client.patch(f"/api/servers/{rid}", json={"env": {"Host": "evil"}})
    assert bad.status_code == 422


# ---- enable ---------------------------------------------------------------------------


async def test_enable_server(real):
    client, _registry, _srv = real
    sid = (await client.post("/api/servers", json=_body())).json()["id"]
    await client.post(f"/api/servers/{sid}/probe")

    er = await client.post(f"/api/servers/{sid}/enable")
    assert er.status_code == 200 and er.json()["enabled"] is True
    # Enabling the server does not approve tools discovered by probe.
    tools = {t["name"]: t["enabled"] for t in (await client.get(f"/api/servers/{sid}/tools")).json()}
    assert tools == {"alpha": False, "beta": False}


async def test_enable_tool_toggle(real):
    client, _registry, _srv = real
    sid = (await client.post("/api/servers", json=_body())).json()["id"]
    await client.post(f"/api/servers/{sid}/probe")
    tools = (await client.get(f"/api/servers/{sid}/tools")).json()
    for tool in tools:
        on = await client.post(f"/api/tools/{tool['id']}/enable", json={"enabled": True})
        assert on.status_code == 200 and on.json()["enabled"] is True
    alpha = next(t for t in tools if t["name"] == "alpha")
    off = await client.post(f"/api/tools/{alpha['id']}/enable", json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    listed = {t["name"]: t["enabled"] for t in (await client.get(f"/api/servers/{sid}/tools")).json()}
    assert listed == {"alpha": False, "beta": True}
    on = await client.post(f"/api/tools/{alpha['id']}/enable", json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True


async def test_disable_server(real):
    client, _registry, _srv = real
    sid = (await client.post("/api/servers", json=_body())).json()["id"]
    await client.post(f"/api/servers/{sid}/probe")
    await client.post(f"/api/servers/{sid}/enable")
    assert (
        await client.post(f"/api/servers/{sid}/enable", json={"enabled": False})
    ).json()["enabled"] is False


async def test_unknown_ids_404(real):
    client, _registry, _srv = real
    missing = uuid.uuid4()
    assert (await client.post(f"/api/servers/{missing}/probe")).status_code == 404
    assert (await client.post(f"/api/servers/{missing}/enable")).status_code == 404
    assert (await client.get(f"/api/servers/{missing}/tools")).status_code == 404
    assert (await client.post("/api/servers/not-a-uuid/probe")).status_code == 422


async def test_mutations_are_audited_without_secrets(real):
    client, _registry, _srv = real
    r = await client.post("/api/servers", json=_body(env={"K": "very-secret"}))
    sid = r.json()["id"]
    await client.post(f"/api/servers/{sid}/probe")
    await client.post(f"/api/servers/{sid}/enable")
    async with async_session() as s:
        evs = (await s.execute(select(AuditEvent))).scalars().all()
    assert {"server.create", "server.probe", "server.enable"} <= {e.action for e in evs}
    assert all(e.actor == "test-operator" for e in evs)
    assert "very-secret" not in json.dumps([e.detail for e in evs])


# ---- operator auth stub fails closed --------------------------------------------------


async def test_operator_auth_stub_denies_by_default(real):
    _client, registry, _srv = real
    async with _http(_app(registry, authed=False)) as anon:
        for method, path in [
            ("post", "/api/servers"),
            ("post", f"/api/servers/{uuid.uuid4()}/probe"),
            ("post", f"/api/servers/{uuid.uuid4()}/enable"),
            ("get", f"/api/servers/{uuid.uuid4()}/tools"),
            ("get", "/api/servers"),
        ]:
            if method == "post":
                r = await anon.post(path, json=_body())
            else:
                r = await anon.get(path)
            assert r.status_code == 401, (method, path)


# ---- scripted fake runner -------------------------------------------------------------


class FakeRunner:
    connected = True
    conn_seq = 1

    def __init__(self, tools=None, error: Exception | None = None, gate=None):
        self.tools = tools if tools is not None else []
        self.error = error
        self.gate = gate  # asyncio.Event: probes block until set
        self.calls: list[dict] = []
        self.active = 0
        self.peak = 0

    async def request(self, op, *, timeout, conn_seq=None, **fields):
        self.calls.append({"op": op, "timeout": timeout, **fields})
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return {"op": op, "ok": True, "tools": self.tools}
        finally:
            self.active -= 1


def _tool(name, schema=None, description=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {"type": "object"},
    }


@pytest.fixture
async def scripted(db, policy_dir):
    runner = FakeRunner([_tool("alpha"), _tool("beta")])
    registry = Registry(
        async_session,
        _vault(),
        runner,
        trusted_path=[str(policy_dir / "bin"), "/usr/bin", "/bin"],
        binary_allowlist=[],
    )
    async with _http(_app(registry)) as http:
        yield http, registry, runner


async def _mk(client, slug="s1"):
    return (await client.post("/api/servers", json=_body(slug=slug))).json()["id"]


async def test_probe_request_shape(scripted):
    client, registry, runner = scripted
    sid = await _mk(client)
    assert (await client.post(f"/api/servers/{sid}/probe")).status_code == 200
    (call,) = runner.calls
    assert call["op"] == "probe"
    assert call["argv"] == ["npx", *FIXTURE_ARGS]
    assert call["server_id"] == sid
    assert call["env"] == {}
    assert call["timeout_s"] == 60  # shared default
    assert call["timeout"] > call["timeout_s"]  # transport timeout leaves room for the runner's
    assert "child_id" not in call  # ephemeral probe, never an invoke child


async def test_reprobe_preserves_operator_enablement(scripted):
    client, _registry, runner = scripted
    sid = await _mk(client)
    await client.post(f"/api/servers/{sid}/probe")
    by_name = {t["name"]: t for t in (await client.get(f"/api/servers/{sid}/tools")).json()}
    assert by_name["alpha"]["enabled"] is False
    assert by_name["beta"]["enabled"] is False
    for name in ("alpha", "beta"):
        on = await client.post(f"/api/tools/{by_name[name]['id']}/enable", json={"enabled": True})
        assert on.status_code == 200

    # beta unchanged, alpha schema changed, gamma new, nothing else missing
    runner.tools = [
        _tool("alpha", {"type": "object", "properties": {"q": {"type": "integer"}}}),
        _tool("beta"),
        _tool("gamma"),
    ]
    r = await client.post(f"/api/servers/{sid}/probe")
    assert r.status_code == 200
    assert r.json()["added"] == 1
    assert r.json()["changed"] == ["alpha"]
    assert r.json()["missing"] == []

    now = {t["name"]: t for t in (await client.get(f"/api/servers/{sid}/tools")).json()}
    assert now["beta"]["enabled"] is True  # unchanged approval sticks
    assert now["alpha"]["enabled"] is False  # schema change needs re-approval
    assert now["alpha"]["schema_hash"] != by_name["alpha"]["schema_hash"]
    assert now["gamma"]["enabled"] is False  # newly discovered
    assert now["alpha"]["id"] == by_name["alpha"]["id"]  # same row

    off = await client.post(f"/api/tools/{now['beta']['id']}/enable", json={"enabled": False})
    assert off.status_code == 200
    r = await client.post(f"/api/servers/{sid}/probe")
    assert r.status_code == 200
    again = {t["name"]: t for t in (await client.get(f"/api/servers/{sid}/tools")).json()}
    assert again["beta"]["enabled"] is False  # operator-disabled survives an unchanged reprobe

    runner.tools = [_tool("beta")]
    r = await client.post(f"/api/servers/{sid}/probe")
    assert sorted(r.json()["missing"]) == ["alpha", "gamma"]
    assert len((await client.get(f"/api/servers/{sid}/tools")).json()) == 3  # rows retained


@pytest.mark.parametrize(
    "error,status,code",
    [
        (RunnerRequestError("runner_unavailable", "down", sent=False), 503, "runner_unavailable"),
        (RunnerRequestError("upstream_timeout", "slow"), 504, "upstream_timeout"),
        (RunnerRequestError("spawn_rejected", "policy", reply={}), 422, "spawn_rejected"),
        (RunnerRequestError("spawn_failed", "exec", reply={}), 502, "spawn_failed"),
        (RunnerRequestError("upstream_crash", "eof", reply={}), 502, "upstream_crash"),
        (RunnerRequestError("child_cap", "busy", reply={}), 429, "child_cap"),
    ],
)
async def test_runner_errors_map_to_http(scripted, error, status, code):
    client, _registry, runner = scripted
    sid = await _mk(client)
    runner.error = error
    r = await client.post(f"/api/servers/{sid}/probe")
    assert r.status_code == status
    assert r.json()["code"] == code
    assert (await client.get(f"/api/servers/{sid}/tools")).json() == []


@pytest.mark.parametrize(
    "tools",
    [
        "nope",
        [{"description": "no name", "inputSchema": {}}],
        [{"name": 5, "inputSchema": {}}],
        [{"name": "a", "inputSchema": "x"}],
        [_tool("dup"), _tool("dup")],
    ],
)
async def test_malformed_probe_result_rejected_atomically(scripted, tools):
    client, _registry, runner = scripted
    sid = await _mk(client)
    runner.tools = tools
    r = await client.post(f"/api/servers/{sid}/probe")
    assert r.status_code == 502
    assert (await client.get(f"/api/servers/{sid}/tools")).json() == []


async def test_missing_input_schema_defaults_to_empty_object_schema(scripted):
    client, _registry, runner = scripted
    sid = await _mk(client)
    runner.tools = [{"name": "bare"}]
    assert (await client.post(f"/api/servers/{sid}/probe")).status_code == 200
    (t,) = (await client.get(f"/api/servers/{sid}/tools")).json()
    assert t["schema"] == {"type": "object"}


async def test_schema_override_survives_probe_and_updates_hash(scripted):
    client, _registry, runner = scripted
    sid = await _mk(client)
    runner.tools = [
        _tool(
            "cal",
            {
                "type": "object",
                "properties": {"query": {"type": "object"}},
            },
        )
    ]
    assert (await client.post(f"/api/servers/{sid}/probe")).status_code == 200
    (tool,) = (await client.get(f"/api/servers/{sid}/tools")).json()
    assert tool["schema_overridden"] is False
    probed_hash = tool["schema_hash"]

    override = {
        "type": "object",
        "properties": {
            "query": {
                "type": "object",
                "properties": {
                    "accountId": {"type": "string"},
                    "calendarIds": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["accountId", "calendarIds"],
            }
        },
        "required": ["query"],
    }
    put = await client.put(f"/api/tools/{tool['id']}/schema-override", json={"schema": override})
    assert put.status_code == 200, put.text
    view = put.json()
    assert view["schema_overridden"] is True
    assert view["schema"] == override
    assert view["probed_schema"]["properties"]["query"]["type"] == "object"
    assert "required" not in view["probed_schema"]
    assert view["schema_hash"] != probed_hash

    # Probe updates probed schema but keeps the override as the effective contract.
    runner.tools = [
        _tool(
            "cal",
            {
                "type": "object",
                "properties": {"query": {"type": "object"}, "extra": {"type": "string"}},
            },
        )
    ]
    assert (await client.post(f"/api/servers/{sid}/probe")).status_code == 200
    after = (await client.get(f"/api/servers/{sid}/tools")).json()[0]
    assert after["schema_overridden"] is True
    assert after["schema"] == override
    assert after["schema_hash"] == view["schema_hash"]
    assert "extra" in after["probed_schema"]["properties"]

    cleared = await client.delete(f"/api/tools/{tool['id']}/schema-override")
    assert cleared.status_code == 200
    assert cleared.json()["schema_overridden"] is False
    assert "extra" in cleared.json()["schema"]["properties"]
    assert "probed_schema" not in cleared.json()


async def test_probe_concurrency_capped_at_two(scripted):
    client, _registry, runner = scripted
    runner.gate = asyncio.Event()
    sids = [await _mk(client, f"s{i}") for i in range(3)]
    tasks = [asyncio.create_task(client.post(f"/api/servers/{s}/probe")) for s in sids[:2]]
    async with asyncio.timeout(3):
        while runner.active < 2:
            await asyncio.sleep(0.01)
    third = await client.post(f"/api/servers/{sids[2]}/probe")
    assert third.status_code == 429
    assert third.json()["code"] == "child_cap"
    runner.gate.set()
    assert [(await t).status_code for t in tasks] == [200, 200]
    assert runner.peak == 2
    # slots are released: a later probe works
    assert (await client.post(f"/api/servers/{sids[2]}/probe")).status_code == 200


async def test_same_server_probe_not_run_twice_concurrently(scripted):
    client, _registry, runner = scripted
    runner.gate = asyncio.Event()
    sid = await _mk(client)
    first = asyncio.create_task(client.post(f"/api/servers/{sid}/probe"))
    async with asyncio.timeout(3):
        while runner.active < 1:
            await asyncio.sleep(0.01)
    second = await client.post(f"/api/servers/{sid}/probe")
    assert second.status_code == 409
    runner.gate.set()
    assert (await first).status_code == 200


async def test_vault_failure_aborts_probe_without_calling_runner(scripted):
    client, registry, runner = scripted
    sid = (await client.post("/api/servers", json=_body(slug="v", env={"K": "v"}))).json()["id"]
    registry.vault = Vault({1: os.urandom(32)})  # different key: stored entry can't decrypt
    r = await client.post(f"/api/servers/{sid}/probe")
    assert r.status_code == 500
    assert r.json()["code"] == "vault_error"
    assert runner.calls == []


async def test_revalidates_argv_at_probe_time(scripted):
    client, registry, runner = scripted
    sid = await _mk(client)
    registry.trusted_path = ["/nonexistent"]  # policy changed since create: npx unresolvable
    r = await client.post(f"/api/servers/{sid}/probe")
    assert r.status_code == 422
    assert r.json()["code"] == "spawn_rejected"
    assert runner.calls == []


async def test_disabling_server_stops_its_children(scripted):
    client, registry, _runner = scripted

    class Slots:
        disabled: list = []

        def disable_server(self, server_id):
            self.disabled.append(server_id)

    registry.slots = Slots()
    sid = await _mk(client)
    await client.post(f"/api/servers/{sid}/enable")
    assert Slots.disabled == []  # enabling does not touch children
    await client.post(f"/api/servers/{sid}/enable", json={"enabled": False})
    assert [str(s) for s in Slots.disabled] == [sid]
