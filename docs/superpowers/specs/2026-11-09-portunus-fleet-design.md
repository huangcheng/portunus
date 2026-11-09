# Portunus — Multi-site Runner Fleet (v1.1)

**Date:** 2026-11-09  
**Status:** draft  
**Depends on:** `2026-11-09-portunus-design.md` (v1 stdio Omni path)  
**Non-goals for this doc:** shared DB with any sibling product; Erlang/OTP dispatcher; copying another product’s deploy inventory or CI.

**One-liner:** Scale from one local runner to many region-tagged runners; schedule MCP work by soft affinity; fall back when preferred sites are empty; surface real upstream failures to agents.

## 1. Problem

v1 assumes **one gateway process + one runner** on a Unix socket. That is enough for local stdio and in-region remotes. It is not enough when:

- Upstream SaaS or package registries are reachable from some networks and not others.
- Operators want **horizontal scale**: several runners in one region, or runners in multiple regions.
- A preferred egress path is down, but a less-preferred path still exists and should try — then report the **actual** connect/reset/timeout error rather than inventing a region failure.

## 2. Goals / non-goals

**Goals**

- Control plane (registry, vault, keys, Omni `/mcp`, operator API) stays a **single logical gateway** (still one worker process in v1.1 unless stated otherwise).
- **N runners**, each advertising `site_id`, `region` tags, and capacity.
- Per registered server: **preferred regions / egress class** (soft affinity).
- Schedule: matching live runners first → other live runners (fallback for scale and availability).
- Prefer-miss is **not** an agent-visible error by itself. Upstream failures keep honest codes/messages (`connection reset`, timeout, DNS, …).
- Operator visibility: audits/metrics show `scheduled_region` and whether preference was missed.
- Sticky child: once started on a runner, stay there until stop/reconcile/grace (no mid-call hop).
- Transport between gateway and runners is authenticated and suitable for **non-local** links (not only Unix socket).
- Dispatcher implementation language: **Python** (same process or a thin sibling module). No BEAM requirement.

**Non-goals**

- Hard refuse-to-schedule when preferred regions are empty (optional strict mode may come later; default is soft).
- Moving the control plane per request.
- Per-tool region pins (server-level only in v1.1).
- Replacing stdio spawn policy, vault, or Omni tool surface.
- Publishing personal hostnames, cloud account ids, or private runbooks in-repo.

## 3. Topology

```text
 agents / operators
        │
        ▼
   ┌────────────┐
   │  Gateway   │  registry, vault, keys, Omni, scheduler
   └─────┬──────┘
         │  authenticated runner links (1..N)
    ┌────┴────┬────────────┐
    ▼         ▼            ▼
 runner A   runner B    runner C
 region=…   region=…    region=…
```

- **Local link (compat):** Unix socket + token + peer-uid checks (v1 behavior) remains supported as a single-site deploy.
- **Remote link:** mutually authenticated channel carrying the **same frame protocol** (length-prefixed JSON). Concrete carriage (TLS, SSH tunnel, WireGuard) is an ops choice; the product interface is “runner connection,” not a specific VPN vendor.

Gateway remains the **sole logical client** of each runner’s accept loop (one connection per runner process). Many runners ⇒ many connections from one gateway.

## 4. Runner identity

On `hello`, each runner sends (additive fields; v1 fields kept):

| Field | Meaning |
|---|---|
| `site_id` | Stable id for this runner install (config) |
| `regions` | List of tags, e.g. `["local"]`, `["region-east"]` — opaque strings, operator-defined |
| `egress_class` | Optional coarse class: `default` \| `restricted-ok` \| `unrestricted` (names illustrative) |
| `capacity` | Optional max concurrent children (else use global defaults) |

Gateway stores live membership in memory (like today’s sole client), rebuilt on reconnect. No requirement to persist membership in DB for v1.1.

## 5. Server affinity

On `servers` (stdio and, later, remote):

| Column / field | Meaning |
|---|---|
| `preferred_regions` | JSON list of region tags; empty = any |
| `preferred_egress` | Optional egress class hint |

**Schedule algorithm (invoke / probe start):**

1. Filter runners that are connected and under cap.
2. Partition into **preferred** (intersection with `preferred_regions` / egress match) and **other**.
3. Pick from preferred (stable order: least-loaded, then `site_id`).
4. If preferred empty, pick from other (same ordering). Record `preferred_miss=true` on the attempt.
5. If no runners at all → existing `runner_unavailable`.

**Fallback policy:** always allow step 4 for v1.1 default. Agents must see upstream truth if the fallback path cannot reach the dependency.

## 6. Wire / slots impact

- `start` / `probe` / `rpc` / `stop` / `kill` / `inventory` semantics unchanged; they target **one** runner connection chosen by the scheduler.
- `SlotManager` gains `site_id` (or connection id) on each slot; reconcile and revoke fan out to the right link.
- Caps: keep per-key and global; add **per-site** cap from runner hello.
- Grace/reap remain **per runner** (each runner owns its children).

## 7. Errors and observability

- Do **not** invent a user-facing “wrong region” success path.
- Map transport failures to existing shared error codes where possible; message may include sanitized OS/HTTP detail (no secrets, no peer tokens).
- Audit (operator): `server_id`, `site_id`, `regions`, `preferred_miss`, outcome code.
- Metrics (optional v1.1): `runner_up{site,region}`, `schedule_preferred_miss_total`, `schedule_fallback_total`.

## 8. Security

- Runner auth: shared token **or** per-site token; prefer per-site tokens in multi-site deploys.
- Remote links: require TLS or equivalent; reject cleartext off-loopback (align with remote SSRF https-only spirit).
- Peer-uid checks apply only to Unix-socket local mode.
- Vault decryption still only on gateway (or on the runner that needs env for spawn — same as v1: gateway sends env literals on `start` after decrypt). Do not broaden secret distribution.
- Docs and examples use **placeholder** regions and hosts only.

## 9. Rollout

| Phase | Deliverable |
|---|---|
| A | Connection multiplex: gateway talks to N local/mock runners; scheduler ignore affinity |
| B | `preferred_regions` on servers + schedule + audit `preferred_miss` |
| C | Remote carriage for one extra runner (documented generic tunnel); e2e with two runners |
| D | Dashboard: show runner sites; edit preferred regions |

v1 single-socket deploy remains the default compose path until Phase C.

## 10. Success criteria

1. Two runners registered; Flomo-class server preferred to region A runs on A when A is up.
2. Stop A; same server schedules on B; invoke either succeeds or fails with a **real** upstream/network error, not a silent hang attributed to “region.”
3. Revoke/ungrant still stops children on the site that owns them.
4. No secrets, private inventory, or personal workflow files required in the git tree.

## 11. Open questions (resolved for v1.1)

| Question | Decision |
|---|---|
| Control plane placement | Operator choice; product does not hard-code a cloud |
| Preferred empty? | Fall back to any live runner; record miss |
| Dispatcher language | Python |
| Strict affinity mode | Deferred |
