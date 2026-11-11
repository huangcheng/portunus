# Deploy and verify checklist

Hosts in this fleet (adjust if inventory changes): gateway on `vultr`; runners on `vultr`, `tencent`, `contabo`. Prefer existing SSH host aliases.

## Sync code

Gateway (bind-mounted overlays + packaged skill_bridge inside the container):

```bash
ROOT=/path/to/portunus
rsync -az --exclude '__pycache__' --exclude '*.pyc' \
  "$ROOT/gateway/portunus_gateway/" vultr:/opt/stacks/portunus/gateway/portunus_gateway/
rsync -az --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' --exclude 'tests' \
  "$ROOT/packages/portunus_skill_bridge/" vultr:/opt/stacks/portunus/packages/portunus_skill_bridge/
rsync -az --delete "$ROOT/dashboard/dist/" vultr:/opt/stacks/portunus/dashboard/dist/

ssh vultr 'docker cp /opt/stacks/portunus/packages/portunus_skill_bridge/. \
  portunus-portunus-1:/app/packages/portunus_skill_bridge/
  docker restart portunus-portunus-1'
```

Runners (**required** after any `portunus_skill_bridge` or `session.py` change):

```bash
for site in vultr tencent contabo; do
  rsync -az --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' --exclude 'tests' \
    "$ROOT/packages/portunus_skill_bridge/" "$site:/tmp/portunus-patch/skill_bridge/"
  # plus session.py / skill tree as needed
  ssh "$site" 'C=portunus-portunus-runner-1
    docker cp /tmp/portunus-patch/skill_bridge/. "$C:/app/packages/portunus_skill_bridge/"
    docker restart "$C"'
done
```

json_cli skill trees → every runner host at `/var/lib/portunus/skills/<dir>` (and empty state dir under `/var/lib/portunus/skill-state/<slug>`).

Dashboard: `cd dashboard && npm run build`, then rsync `dist/` (and `public/icons/apps/` if icons are not already in the build output).

## Refresh catalog

After gateway is up:

```bash
docker exec portunus-portunus-1 python -c '
import asyncio
from portunus_gateway.catalog import CatalogStore
from portunus_gateway.db.session import async_session
async def main():
    print("seed", await CatalogStore(async_session).ensure_defaults())
asyncio.run(main())
'
```

Confirm the slug row exists and `spec.manifest.auth` matches the packaged YAML.

## Operator smoke

1. Wait until `GET /api/status` shows `runner_connected: true`.
2. Connect the app in the dashboard (or `POST /api/servers/{id}/skill-auth` with `action: login` / `status`).
3. Expect `logged_in: true` (platform auth) or vaulted secrets saved (bearer).
4. `POST /api/servers/{id}/probe` → tool list non-empty.
5. Enable server (`POST .../enable` with `{"enabled":true}`) so agents see it — dashboard Probe does this automatically.

## Failure signatures

| Symptom | Likely cause |
|---|---|
| Probe `upstream_crash` / Manifest validation error on runner | Stale `portunus_skill_bridge` on that runner |
| Auth works, tools = 0 | Probe never succeeded or server left disabled |
| oauth_link `授权码无效` | Operator scanned authorize URL with vendor 扫一扫 — use Open/Copy browser flow |
| Login URL missing / job not sticky | `browser_capture` off, or status polls not hitting the same runner site |
| Tiny icon | Slug missing from 48×48 `.app-brand-image` CSS list |
| Catalog missing new oauth/http app on live DB | `ensure_defaults` only auto-adds **skill_bridge** after first seed — create via API |

## Local package check on a runner

```bash
docker exec portunus-portunus-runner-1 python -c '
from portunus_skill_bridge.manifest import load_manifest
from pathlib import Path
import portunus_skill_bridge
p = Path(portunus_skill_bridge.__file__).resolve().parent.parent / "manifests" / "<slug>.example.yaml"
m = load_manifest(p)
print(m.name, m.auth.type, len(m.tools))
'
```
