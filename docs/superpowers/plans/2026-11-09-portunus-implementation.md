# Portunus v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a self-hosted MCP gateway that registers pinned `npx`/`uvx` and remote MCP servers, stores operator secrets, issues agent keys with grants, and exposes Omni discovery at `/mcp`.

**Architecture:** Two processes — FastAPI gateway (registry, vault, keys, Omni `/mcp`, remote proxy, slot FSM) and a runner that listens on a Unix socket, spawns stdio MCP children, and owns grace/reap. Shared library holds argv grammar, wire framing, and error codes. SQLite first; Postgres-compatible schema.

**Tech Stack:** Python 3.12+, FastAPI, official MCP Python SDK (Streamable HTTP), SQLAlchemy 2 + Alembic, AES-GCM vault, React + TanStack Router + shadcn (dashboard tasks), pytest + httpx + anyio.

**Spec:** `docs/superpowers/specs/2026-11-09-portunus-design.md`

## Global Constraints

- Python ≥ 3.12; single gateway worker process (no multi-worker).
- Agent keys: `pm-<32 base64url>.<43 base64url>`; verify `HMAC-SHA256(key=pepper, msg=secret)`.
- Omni tools only: `portunus_discover_servers`, `portunus_discover_tools`, `portunus_get_tool_schema`, `portunus_invoke_tool`.
- Closed argv grammar from spec §6; dual validation (gateway + runner).
- Wire: big-endian u32 length + UTF-8 JSON; max frame 5MiB; ops from spec §6.2.
- Error codes exactly as spec §6.4.
- Defaults: invoke 60s, probe 60s, start ack 15s, grace 30s, heartbeat 10s; caps 32 children/key, 256 global invoke children, 2 probes.
- Dev mode may run same-UID without setuid; install path uses `portunus` / `portunus-runner` / `portunus-mcp` as specified.
- Audits and secrets never land in the git repo (`.env`, keyring, `/tmp` audits).

## Review Focus

- Revoke while a start is in flight — expect generation re-check and `pending_stop`, never a live child for a revoked key.
- Same-epoch reconnect after ungrant during grace — inventory reconcile must kill that child, not adopt it.
- `npx pkg@latest` and `npx -c` — both `spawn_rejected` at gateway save and runner exec.
- Oversized tool result (>1MiB) — agent sees `payload_too_large`, never a truncated success.
- Heartbeat gap >30s — runner closes socket, starts grace, SIGKILL+reap if no reconnect; gateway retries connect.

---

## File Structure

```text
portunus/
  packages/portunus_shared/     # argv grammar, wire codec, errors, constants
  runner/                       # socket server, spawn, grace, inventory
  gateway/                      # FastAPI app, DB, vault, slots, omni, remote, operator API
  dashboard/                    # React SPA (later tasks)
  docs/
  pyproject.toml                # workspace / uv members
  docker-compose.yml
```

| Path | Responsibility |
|---|---|
| `packages/portunus_shared/portunus_shared/errors.py` | Error code enum / strings |
| `packages/portunus_shared/portunus_shared/argv.py` | Closed argv parse/validate |
| `packages/portunus_shared/portunus_shared/wire.py` | Frame encode/decode + message types |
| `runner/portunus_runner/main.py` | Entrypoint, listen loop |
| `runner/portunus_runner/session.py` | Sole client, heartbeats, grace |
| `runner/portunus_runner/spawn.py` | Policy load, validate, setuid/dev spawn |
| `runner/portunus_runner/children.py` | Child table, inventory, reap |
| `gateway/portunus_gateway/config.py` | Settings from env |
| `gateway/portunus_gateway/db/` | Models + Alembic |
| `gateway/portunus_gateway/vault.py` | AES-GCM seal/open |
| `gateway/portunus_gateway/keys.py` | Mint/verify/revoke agent keys |
| `gateway/portunus_gateway/authz.py` | In-process authz store + generation |
| `gateway/portunus_gateway/slots.py` | Reserve/start/commit/rollback + reconcile |
| `gateway/portunus_gateway/runner_client.py` | Long-lived client |
| `gateway/portunus_gateway/registry.py` | Servers/tools CRUD + probe orchestration |
| `gateway/portunus_gateway/omni.py` | FastMCP `/mcp` four tools |
| `gateway/portunus_gateway/remote.py` | Remote MCP proxy + SSRF controls |
| `gateway/portunus_gateway/operator_api.py` | Operator REST + sessions |
| `dashboard/` | SPA against operator API |

---

### Task 1: Shared package — errors, constants, argv grammar

**Files:**
- Create: `packages/portunus_shared/pyproject.toml`
- Create: `packages/portunus_shared/portunus_shared/__init__.py`
- Create: `packages/portunus_shared/portunus_shared/errors.py`
- Create: `packages/portunus_shared/portunus_shared/argv.py`
- Create: `packages/portunus_shared/portunus_shared/constants.py`
- Test: `packages/portunus_shared/tests/test_argv.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `ERROR_CODES: frozenset[str]` including every §6.4 code
  - `validate_stdio_command(command: str, args: list[str], *, trailing_allowlist: list[str], trusted_path: list[str], binary_allowlist: list[str]) -> None` raises `ArgvError(code="spawn_rejected", message=...)`
  - `DEFAULTS` dataclass: invoke_timeout_s=60, start_ack_s=15, grace_s=30, heartbeat_s=10, max_children_per_key=32, max_children_global=256, max_probes=2

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from portunus_shared.argv import ArgvError, validate_stdio_command

POLICY = {
    "trusted_path": ["/usr/bin", "/bin"],
    "binary_allowlist": [],
}

def test_npx_pinned_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: "/usr/bin/npx")
    validate_stdio_command(
        "npx",
        ["-y", "--ignore-scripts", "demo@1.2.3"],
        trailing_allowlist=[],
        trusted_path=POLICY["trusted_path"],
        binary_allowlist=[],
    )

def test_npx_latest_rejected():
    with pytest.raises(ArgvError) as e:
        validate_stdio_command(
            "npx",
            ["--ignore-scripts", "demo@latest"],
            trailing_allowlist=[],
            trusted_path=["/usr/bin"],
            binary_allowlist=[],
        )
    assert e.value.code == "spawn_rejected"

def test_npx_dash_c_rejected():
    with pytest.raises(ArgvError) as e:
        validate_stdio_command(
            "npx",
            ["-c", "echo hi"],
            trailing_allowlist=[],
            trusted_path=["/usr/bin"],
            binary_allowlist=[],
        )
    assert e.value.code == "spawn_rejected"

def test_uvx_ok(monkeypatch):
    monkeypatch.setattr("portunus_shared.argv.which", lambda c, path=None: "/usr/bin/uvx")
    validate_stdio_command(
        "uvx",
        ["pkg==1.0.0"],
        trailing_allowlist=[],
        trusted_path=["/usr/bin"],
        binary_allowlist=[],
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/portunus_shared && uv run pytest tests/test_argv.py -v`  
Expected: FAIL (module not found / import error)

- [ ] **Step 3: Implement package**

Implement `ArgvError`, full grammar from spec §6 (npx/uvx/absolute), and `ERROR_CODES` frozenset with all §6.4 strings.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_argv.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/portunus_shared
git commit -m "feat(shared): add argv grammar and error codes"
```

---

### Task 2: Shared wire codec

**Files:**
- Create: `packages/portunus_shared/portunus_shared/wire.py`
- Test: `packages/portunus_shared/tests/test_wire.py`

**Interfaces:**
- Consumes: `portunus_shared.errors`
- Produces:
  - `encode_frame(obj: dict) -> bytes` — BE u32 + UTF-8 JSON; raise if body > 5MiB
  - `async def read_frame(reader: asyncio.StreamReader) -> dict`
  - Typed dict helpers for ops: `hello`, `inventory`, `start`, `stop`, `kill`, `probe`, `rpc`, `cancel`, `heartbeat`, `child_exited`

- [ ] **Step 1: Write the failing test**

```python
import asyncio
from portunus_shared.wire import encode_frame, read_frame

def test_roundtrip():
    raw = encode_frame({"version": 1, "op": "heartbeat", "request_id": "a"})
    assert int.from_bytes(raw[:4], "big") == len(raw) - 4

    async def _go():
        r, w = await asyncio.open_connection(host="127.0.0.1", port=0)  # use StreamReader feed
    # Prefer feeding BytesIO-style reader:
    class R:
        def __init__(self, data): self._data = data
        async def readexactly(self, n):
            out, self._data = self._data[:n], self._data[n:]
            if len(out) < n: raise asyncio.IncompleteReadError(out, n)
            return out
    obj = asyncio.run(read_frame(R(raw)))
    assert obj["op"] == "heartbeat"

def test_oversize_rejected():
    import pytest
    from portunus_shared.wire import FrameError
    with pytest.raises(FrameError):
        encode_frame({"blob": "x" * (5 * 1024 * 1024)})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wire.py -v`  
Expected: FAIL import

- [ ] **Step 3: Implement `wire.py`**

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit**

```bash
git add packages/portunus_shared
git commit -m "feat(shared): add length-prefixed JSON wire codec"
```

---

### Task 3: Vault (AES-GCM)

**Files:**
- Create: `gateway/pyproject.toml` (depends on `portunus_shared`)
- Create: `gateway/portunus_gateway/vault.py`
- Create: `gateway/portunus_gateway/config.py`
- Test: `gateway/tests/test_vault.py`

**Interfaces:**
- Consumes: env `PORTUNUS_SECRETS_KEY` as `k1:<base64-32-bytes>`
- Produces:
  - `Vault.from_keyring(keyring: str) -> Vault`
  - `Vault.seal(plaintext: bytes, *, vault_entry_id: str, key_version: int) -> bytes`
  - `Vault.open(blob: bytes, *, vault_entry_id: str, key_version: int) -> bytes`

- [ ] **Step 1: Write failing tests**

```python
from portunus_gateway.vault import Vault
import os, base64

def test_seal_open_roundtrip():
    key = base64.b64encode(os.urandom(32)).decode()
    v = Vault.from_keyring(f"k1:{key}")
    pt = b"sk-secret"
    ct = v.seal(pt, vault_entry_id="e1", key_version=1)
    assert ct != pt
    assert v.open(ct, vault_entry_id="e1", key_version=1) == pt

def test_aad_mismatch_fails():
    import pytest
    from portunus_gateway.vault import VaultError
    key = base64.b64encode(os.urandom(32)).decode()
    v = Vault.from_keyring(f"k1:{key}")
    ct = v.seal(b"x", vault_entry_id="e1", key_version=1)
    with pytest.raises(VaultError):
        v.open(ct, vault_entry_id="e2", key_version=1)
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement AES-256-GCM with random nonce; AAD = `f"{vault_entry_id}|{key_version}".encode()`**

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): add AES-GCM vault with AAD binding"
```

---

### Task 4: Database models — servers, tools, vault entries, agent keys, grants, sessions

**Files:**
- Create: `gateway/portunus_gateway/db/models.py`
- Create: `gateway/portunus_gateway/db/session.py`
- Create: `gateway/alembic/` (initial migration)
- Test: `gateway/tests/test_models.py`

**Interfaces:**
- Consumes: SQLAlchemy 2, SQLite URL `sqlite+aiosqlite:///./data/portunus.db` for tests
- Produces tables: `servers`, `tools`, `vault_entries`, `agent_keys`, `grants`, `operator_sessions`, `audit_events` with UUID PKs; `servers.slug` unique casefold; `agent_keys.key_id` unique; `grants(key_id, server_id)` unique

- [ ] **Step 1: Write failing test that creates a server + tool + key + grant and queries them**

```python
import pytest
from portunus_gateway.db.session import engine, async_session
from portunus_gateway.db.models import Base, Server, Tool, AgentKey, Grant

@pytest.mark.anyio
async def test_server_tool_grant_roundtrip():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        srv = Server(name="demo", slug="demo", kind="stdio", enabled=False,
                     command="npx", args=["--ignore-scripts", "demo@1.0.0"])
        s.add(srv)
        await s.flush()
        s.add(Tool(server_id=srv.id, upstream_name="ping", schema={}, schema_hash="h", enabled=False))
        key = AgentKey(key_id="a"*32, digest=b"\x00"*32)
        s.add(key)
        await s.flush()
        s.add(Grant(key_id=key.key_id, server_id=srv.id))
        await s.commit()
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement models + `create_all` for tests; Alembic revision for real migrations**

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): add SQLAlchemy models and initial schema"
```

---

### Task 5: Agent keys + in-process authz store

**Files:**
- Create: `gateway/portunus_gateway/keys.py`
- Create: `gateway/portunus_gateway/authz.py`
- Test: `gateway/tests/test_keys.py`
- Test: `gateway/tests/test_authz.py`

**Interfaces:**
- Consumes: vault pepper file path from config; DB `AgentKey` / `Grant`
- Produces:
  - `mint_agent_key(*, expires_at: datetime | None) -> tuple[str, AgentKey]`  # full token once
  - `verify_agent_token(token: str) -> AgentKey`  # raises with `unauthorized` / `revoked`
  - `AuthzStore.reload_from_db()` / `bump()` / `is_revoked(key_id)` / `has_grant(key_id, server_id)` / `generation: int`
  - `revoke_key(key_id)` sets `revoked_at`, bumps generation

- [ ] **Step 1: Write failing tests**

```python
def test_mint_and_verify(pepper_file, db):
    token, row = mint_agent_key(expires_at=None)
    assert token.startswith("pm-")
    parts = token[3:].split(".")
    assert len(parts[0]) == 32 and len(parts[1]) == 43
    assert verify_agent_token(token).key_id == row.key_id

def test_revoke_fails_verify(db):
    token, row = mint_agent_key(expires_at=None)
    revoke_key(row.key_id)
    with pytest.raises(AuthError) as e:
        verify_agent_token(token)
    assert e.value.code == "revoked"
```

Also test Review Focus: after revoke, `AuthzStore.generation` increases and `has_grant` still readable but verify fails.

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement mint (secrets.token_bytes), HMAC verify, authz store**

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): agent key mint/verify and authz store"
```

---

### Task 6: Runner — listen, sole client, heartbeat, grace kill

**Files:**
- Create: `runner/pyproject.toml`
- Create: `runner/portunus_runner/main.py`
- Create: `runner/portunus_runner/session.py`
- Create: `runner/portunus_runner/children.py`
- Test: `runner/tests/test_grace.py`

**Interfaces:**
- Consumes: `portunus_shared.wire`, defaults
- Produces: runner process listening on `PORTUNUS_SOCKET` (default `/tmp/portunus-test.sock` in tests); on heartbeat gap or EOF closes socket, 30s grace, then SIGKILL+reap all children and empty inventory

- [ ] **Step 1: Write failing integration test**

```python
@pytest.mark.anyio
async def test_grace_expiry_kills_child(tmp_path):
    # start runner with heartbeat_s=0.05, grace_s=0.1 (test overrides)
    # connect, hello, start a sleep child (absolute /bin/sleep with trailing allowlist empty → use a fixture binary)
    # stop sending heartbeats; expect child dead after grace and inventory empty on reconnect
```

For unit speed, inject a fake child process handle.

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement session loop: encode/decode frames; track `last_heartbeat`; grace timer; `children.kill_all_and_reap()` before empty inventory**

Dev mode: skip peer-cred uid checks when `PORTUNUS_DEV=1`; still require token.

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runner): socket session with heartbeat grace and reap"
```

---

### Task 7: Runner — argv re-validation + spawn (dev mode)

**Files:**
- Create: `runner/portunus_runner/spawn.py`
- Create: `runner/portunus_runner/policy.py`
- Modify: `runner/portunus_runner/session.py` (handle `start` / `stop` / `kill` / `probe`)
- Test: `runner/tests/test_spawn_argv.py`

**Interfaces:**
- Consumes: `validate_stdio_command`, policy JSON
- Produces: `start` op spawns process with env literals; emits `child_exited` on death; rejects bad argv with `spawn_rejected`

- [ ] **Step 1: Failing test — start with `demo@latest` returns error frame `spawn_rejected`; start with pinned sleep/true fixture succeeds and `stop` yields `child_exited`**

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement spawn**

In `PORTUNUS_DEV=1`, do not setuid; still close surplus fds. Production path implements setuid sequence from spec (may skip in CI without capabilities; gate behind `PORTUNUS_DROP_PRIVS=1`).

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runner): validate argv and spawn stdio children"
```

---

### Task 8: Gateway runner client + slot FSM

**Files:**
- Create: `gateway/portunus_gateway/runner_client.py`
- Create: `gateway/portunus_gateway/slots.py`
- Test: `gateway/tests/test_slots.py`

**Interfaces:**
- Consumes: wire codec; authz store; runner socket
- Produces:
  - `SlotManager.reserve_and_start(server_id, key_id, start_payload) -> child_id`
  - `SlotManager.on_child_exited(child_id)`
  - `SlotManager.reconcile(inventory, *, authz_ Predicates)`
  - Caps enforced; revoke mid-start leaves `pending_stop`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.anyio
async def test_cap_per_key(slot_manager, fake_runner):
    # start 32 children for one key — ok; 33rd raises child_cap

@pytest.mark.anyio
async def test_revoke_during_start(slot_manager, fake_runner, authz):
    # begin start; revoke key before commit; expect pending_stop and no live adopt on reconcile

@pytest.mark.anyio
async def test_reconcile_drops_ungranted(slot_manager, authz):
    # inventory lists child; ungrant server; reconcile issues kill and frees after exit
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement SlotManager + RunnerClient (long-lived; retry connect)**

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): runner client and child slot FSM"
```

---

### Task 9: Registry API — create stdio server, probe via runner, enable tools

**Files:**
- Create: `gateway/portunus_gateway/registry.py`
- Create: `gateway/portunus_gateway/operator_api.py` (servers/tools routes; stub operator auth for now with test dependency override)
- Test: `gateway/tests/test_registry_probe.py`

**Interfaces:**
- Consumes: slots/runner for `probe`; argv validate on create; vault for env literals
- Produces: REST `POST /api/servers`, `POST /api/servers/{id}/probe`, `POST /api/servers/{id}/enable`, `POST /api/tools/{id}/enable`

Probe uses runner `probe` op; stores tools with `enabled=False`; new tools stay disabled.

- [ ] **Step 1: Failing test with fake MCP stdio fixture that speaks minimal JSON-RPC `tools/list`**

```python
@pytest.mark.anyio
async def test_probe_lists_tools_disabled(client, runner_with_fixture):
    r = await client.post("/api/servers", json={
        "name": "fix", "slug": "fix", "kind": "stdio",
        "command": "npx", "args": ["--ignore-scripts", "fixture@1.0.0"],
        "env": {},
    })
    assert r.status_code == 201
    # override command to absolute fixture binary in test via allowlist
    pr = await client.post(f"/api/servers/{r.json()['id']}/probe")
    assert pr.status_code == 200
    tools = (await client.get(f"/api/servers/{r.json()['id']}/tools")).json()
    assert tools and all(t["enabled"] is False for t in tools)
```

- [ ] **Step 2–4: TDD implement**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): server registry and stdio probe"
```

---

### Task 10: Omni `/mcp` — four tools end-to-end with agent key

**Files:**
- Create: `gateway/portunus_gateway/omni.py`
- Create: `gateway/portunus_gateway/app.py`
- Test: `gateway/tests/test_omni.py`

**Interfaces:**
- Consumes: authz, registry, slots, MCP SDK Streamable HTTP
- Produces: `POST /mcp` (Streamable HTTP) requiring `Authorization: Bearer pm-...`
  - `portunus_discover_servers` / `portunus_discover_tools` (cursor pages 50/100)
  - `portunus_get_tool_schema`
  - `portunus_invoke_tool` — validate args vs stored schema; size-cap results

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.anyio
async def test_tools_list_only_four_meta(mcp_client):
    tools = await mcp_client.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "portunus_discover_servers",
        "portunus_discover_tools",
        "portunus_get_tool_schema",
        "portunus_invoke_tool",
    ]

@pytest.mark.anyio
async def test_invoke_requires_grant(mcp_client, server_enabled_without_grant):
    with pytest.raises(Exception):
        await mcp_client.call_tool("portunus_invoke_tool", {...})

@pytest.mark.anyio
async def test_happy_path_discover_and_invoke(mcp_client_with_grant):
    servers = await mcp_client_with_grant.call_tool("portunus_discover_servers", {"query": ""})
    ...
```

Also test Review Focus oversized result → `payload_too_large`.

- [ ] **Step 2–4: Implement Omni with FastMCP or raw MCP server mount; wire auth middleware**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): Omni /mcp discover and invoke path"
```

---

### Task 11: Remote MCP proxy + SSRF controls

**Files:**
- Create: `gateway/portunus_gateway/remote.py`
- Test: `gateway/tests/test_remote_ssrf.py`

**Interfaces:**
- Consumes: vault; httpx
- Produces: remote probe/invoke with default-deny private IPs (loopback allowlist exception); strip creds on cross-origin redirect; https-only off-loopback

- [ ] **Step 1: Failing tests — connect to `169.254.169.254` rejected; loopback http allowlisted in test config succeeds**

- [ ] **Step 2–4: Implement**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): remote MCP proxy with SSRF controls"
```

---

### Task 12: Operator sessions + vault/key REST + minimal dashboard shell

**Files:**
- Create: `gateway/portunus_gateway/operator_auth.py`
- Modify: `gateway/portunus_gateway/operator_api.py`
- Create: `dashboard/package.json`, `dashboard/src/` (login, servers list, create stdio server, mint key)
- Test: `gateway/tests/test_operator_auth.py`

**Interfaces:**
- Consumes: argon2id password from `PORTUNUS_OPERATOR_PASSWORD`; opaque session cookie
- Produces: login/logout; CSRF on mutations; step-up token endpoint; UI to exercise success criteria 1–2 manually

- [ ] **Step 1: Failing test — 5 failed logins lock out for 15 minutes per IP**

- [ ] **Step 2–4: Implement auth + thin SPA calling the API**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: operator auth and minimal dashboard for servers/keys"
```

---

### Task 13: Compose + README quickstart + e2e script

**Files:**
- Create: `docker-compose.yml`
- Create: `README.md`
- Create: `scripts/e2e_stdio_omni.sh`
- Create: `deploy/runner-policy.json.example`

**Interfaces:**
- Produces: documented env vars; compose with `portunus`, `portunus-runner`, optional postgres; e2e script that mints key, registers fixture server, probes, enables, invokes via `/mcp`

- [ ] **Step 1: Write e2e script that fails if Omni invoke does not return expected fixture result**

- [ ] **Step 2: Run script against local dev — FAIL until services wired**

- [ ] **Step 3: Wire compose + README; make e2e pass in dev mode**

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: compose, README, and stdio Omni e2e script"
```

---

## Spec coverage (self-review)

| Spec area | Task |
|---|---|
| Argv grammar / dual validation | 1, 7 |
| Wire protocol | 2, 6–8 |
| Vault | 3, 9, 12 |
| Agent keys / grants / revoke | 5, 8, 10 |
| Slots / grace / reconcile | 6–8 |
| Omni `/mcp` | 10 |
| Remote + SSRF | 11 |
| Dashboard / operator auth | 12 |
| Success criteria / e2e | 13 |
| Probe disabled-by-default | 9 |
| Error codes | 1, threaded through 6–11 |

**Placeholders:** none intentional.  
**Review Focus lines** mapped to tests in Tasks 5, 7, 8, 10, 6 respectively.
