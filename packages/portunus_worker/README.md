# portunus-worker

Temporal control-plane worker for Portunus.

## Workflows

| ID pattern | Purpose |
|---|---|
| `oauth-refresh:{server_id}` | Proactive OAuth access-token refresh |
| `server-probe:{server_id}` | Periodic active probe |
| `connect-ensure:{server_id}` | Connect → probe → enable → start children |
| `fleet-skills-sync:*` | Fleet skill-tree rsync (`deploy/fleet-skills.sh`) |

## Run locally

```bash
docker compose --profile temporal up -d
# gateway needs TEMPORAL_HOST=temporal:7233
```

Worker entrypoint: `portunus-worker` (same image as gateway).

Env: `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE` (default `default`), `TEMPORAL_TASK_QUEUE` (`portunus-control`),
`PORTUNUS_FLEET_HOSTS`, `PORTUNUS_PROBE_INTERVAL_S`, `PORTUNUS_OAUTH_REFRESH_SKEW_S`.
