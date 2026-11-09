# Portunus Fleet (multi-site runners) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one Portunus gateway talk to N runners with soft region affinity, fallback when preferred sites are empty, and honest upstream errors.

**Architecture:** Keep the Python gateway as control plane. Introduce a `RunnerPool` that owns many `RunnerClient` links. Extend runner `hello` with site/region metadata. Add `preferred_regions` on servers and a pure scheduler used by slots/probe/invoke. Unix-socket single-runner remains the default path.

**Tech Stack:** Existing Python 3.12+, asyncio, FastAPI gateway, runner frame protocol (`portunus_shared.wire`), pytest/anyio. No new language runtime.

**Spec:** `docs/superpowers/specs/2026-11-09-portunus-fleet-design.md`

## Global Constraints

- Do not commit secrets, private inventory, personal hostnames, or private automation.
- Docs/examples use placeholder regions only (`region-a`, `region-b`, `local`).
- Soft affinity by default: preferred empty ⇒ schedule any live runner; record `preferred_miss`.
- Same frame protocol; additive hello fields only.
- Sticky child per site until stop/reconcile/grace.
- v1 single-socket compose keeps working until remote carriage lands.
- Python dispatcher only.

## Review Focus

- Preferred runner down ⇒ work still schedules elsewhere; agent sees real upstream failure if that path cannot reach the dependency — test in Task 4.
- Child started on site A is never adopted by site B inventory — test in Task 3.
- Revoke/ungrant stops the child on the owning site — test in Task 3.
- Empty runner pool still returns `runner_unavailable` — test in Task 2.
- Hello without fleet fields still works (v1 compat) — test in Task 1.

---

## File Structure

| Path | Responsibility |
|---|---|
| `gateway/portunus_gateway/runner_pool.py` | N clients, membership, pick connection |
| `gateway/portunus_gateway/scheduler.py` | Pure preferred/fallback ranking |
| `gateway/portunus_gateway/runner_client.py` | Hello parse for site/regions (compat) |
| `gateway/portunus_gateway/slots.py` | Slot carries `site_id`; route stop/rpc |
| `gateway/portunus_gateway/db/models.py` | `preferred_regions` on `Server` |
| `gateway/portunus_gateway/registry.py` / `omni.py` | Pass affinity into start/probe |
| `runner/portunus_runner/session.py` / `main.py` | Advertise site/regions on hello |
| `gateway/tests/test_scheduler.py` | Ranking unit tests |
| `gateway/tests/test_runner_pool.py` | Multiplex + fallback |
| `docs/.../fleet-design.md` | Spec (already written) |

---

### Task 1: Runner hello fleet metadata (compat)

**Files:**
- Modify: `runner/portunus_runner/main.py`, `runner/portunus_runner/session.py`
- Modify: `gateway/portunus_gateway/runner_client.py`
- Test: `runner/tests/test_grace.py` (compat) + `gateway/tests/test_runner_client.py`

**Interfaces:**
- Consumes: existing hello/ack
- Produces: runner hello may include `site_id: str`, `regions: list[str]`, optional `egress_class: str`, optional `capacity: int`. Gateway stores them on the live session. Missing fields ⇒ `site_id="local"`, `regions=["local"]`.

- [ ] **Step 1: Failing test — gateway records regions from hello**

```python
@pytest.mark.anyio
async def test_hello_records_site_and_regions(fake_or_real_runner):
    # runner configured with PORTUNUS_SITE_ID=site-a PORTUNUS_REGIONS=region-a,region-b
    # after connect, client.site_id == "site-a" and "region-a" in client.regions
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement env-driven advertisement + client parse (defaults for v1)**

- [ ] **Step 4: Run — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runner): advertise site_id and regions on hello"
```

---

### Task 2: Pure scheduler

**Files:**
- Create: `gateway/portunus_gateway/scheduler.py`
- Test: `gateway/tests/test_scheduler.py`

**Interfaces:**
- Produces: `pick_runner(candidates: Sequence[RunnerInfo], *, preferred_regions: Sequence[str]) -> ScheduleDecision | None`
- `ScheduleDecision(site_id, preferred_miss: bool)`
- Empty candidates ⇒ `None` (caller maps to `runner_unavailable`)

- [ ] **Step 1: Tests — prefer match; fallback sets preferred_miss; empty → None; stable least-loaded**

- [ ] **Step 2–4: TDD implement**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): soft region affinity scheduler"
```

---

### Task 3: RunnerPool multiplex + slot site_id

**Files:**
- Create: `gateway/portunus_gateway/runner_pool.py`
- Modify: `gateway/portunus_gateway/slots.py`, `gateway/portunus_gateway/main.py`
- Test: `gateway/tests/test_runner_pool.py`, extend `gateway/tests/test_slots.py`

**Interfaces:**
- `RunnerPool.add/remove`, `connected()`, `request(site_id, op, payload)`, `pick(...)` via scheduler
- Slots store `site_id`; stop/kill/rpc use that connection
- Single-runner bootstrap: pool of one wrapping today’s client (compat)

- [ ] **Step 1: Tests — two fake runners; start pins site; stop hits owning site; empty pool errors**

- [ ] **Step 2–4: Implement**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): runner pool with per-slot site routing"
```

---

### Task 4: preferred_regions on servers + wire into probe/invoke

**Files:**
- Modify: `gateway/portunus_gateway/db/models.py` + Alembic migration
- Modify: `gateway/portunus_gateway/registry.py`, `operator_api.py`, `omni.py`
- Test: `gateway/tests/test_registry_probe.py`, `gateway/tests/test_omni.py` (or focused fleet e2e test)

**Interfaces:**
- Server create/update accepts `preferred_regions: list[str]` (default `[]`)
- Probe/start/invoke pass server preferences into pool.pick
- Audit or structured log field `preferred_miss` on schedule

- [ ] **Step 1: Test — server prefers region-a; only region-b up ⇒ schedules B with preferred_miss; invoke surfaces real child error if fixture fails**

- [ ] **Step 2–4: Implement**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gateway): preferred_regions soft affinity on servers"
```

---

### Task 5: Docs + dual-runner local e2e (no private inventory)

**Files:**
- Modify: `README.md` (Limitations / optional fleet note)
- Create: `scripts/e2e_two_runners.sh` using only local temp sockets and placeholder regions
- Modify: `.env.example` with commented `PORTUNUS_SITE_ID` / `PORTUNUS_REGIONS`

**Interfaces:**
- Script starts two runners (`site-a`/`region-a`, `site-b`/`region-b`) + one gateway; asserts preference then kill-a fallback

- [ ] **Step 1: Write script that fails until pool wiring works**

- [ ] **Step 2–3: Make it pass on localhost only**

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: dual-runner soft-affinity e2e and docs"
```

---

## Spec coverage

| Spec area | Task |
|---|---|
| Hello metadata | 1 |
| Soft schedule + miss | 2, 4 |
| N links + sticky site | 3 |
| Honest fallback errors | 4, 5 |
| No secrets in tree | 5 + SECURITY.md |

**Placeholders:** none intentional. Remote TLS carriage beyond local sockets is Phase C deferred (document only in README until a later plan).
