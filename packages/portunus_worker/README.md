# portunus-worker

Temporal control-plane worker for Portunus.

## Workflows

| ID pattern | Purpose |
|---|---|
| `oauth-refresh` | Shared proactive OAuth refresher (one workflow; per-server refresh is an activity so one bad provider cannot stall the queue; `kick` after tokens stored; gateway boot ensures it and terminates legacy `oauth-refresh:{uuid}`) |
| `server-probe` | Shared active-probe scheduler (one workflow; `list_probe_plan` then per-server `probe_server` on `portunus-probe` inside the gateway sole-client pool; `probe_now(server_id)` prioritizes; boot terminates legacy `server-probe:{uuid}`). Site pick uses portable `preferred_regions` (e.g. `overseas`) + SiteHealth failover in `Registry.probe`. |
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
