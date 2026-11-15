# portunus-worker

Temporal control-plane worker for Portunus.

## Workflows

| ID pattern | Purpose |
|---|---|
| `oauth-refresh` | Shared proactive OAuth refresher (one workflow; per-server refresh is an activity so one bad provider cannot stall the queue; `kick` after tokens stored; gateway boot ensures it and terminates legacy `oauth-refresh:{uuid}`) |
| `server-probe:{server_id}` | Periodic active probe (gateway boot ensures one per enabled server; `probe_now` wakes early). The `probe_server` activity runs **inside the gateway** so it shares the sole-client RunnerPool; workflows stay on `portunus-worker`. |
| `connect-ensure:{server_id}` | Connect → probe → enable → start children |
| `fleet-skills-sync:*` | Fleet skill-tree rsync (`deploy/fleet-skills.sh`) |

## Run locally

```bash
docker compose --profile temporal up -d
# gateway needs TEMPORAL_HOST=temporal:7233
```

Worker entrypoint: `portunus-worker` (same image as gateway).

Env: `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE` (default `default`), `TEMPORAL_TASK_QUEUE` (`portunus-control`),
`TEMPORAL_PROBE_TASK_QUEUE` (`portunus-probe`, gateway-embedded probe activities),
`PORTUNUS_FLEET_HOSTS`, `PORTUNUS_PROBE_INTERVAL_S`, `PORTUNUS_OAUTH_REFRESH_SKEW_S`.
