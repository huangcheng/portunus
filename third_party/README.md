# Third-party skill bundles

Vendored runtimes used by skill-bridge catalog apps (not MCP servers themselves).

- `guangyapan-drive/` — 光鸭云盘 CLI skill v1.0.2 (bundled), deployed to runners at `/var/lib/portunus/skills/guangyapan-drive`.
- `quarkclouddrive/` — 夸克网盘 CLI skill v1.0.25 (bundled), deployed to runners at `/var/lib/portunus/skills/quarkclouddrive`.
- `proton-drive/` — Proton Drive wrapper around the official `proton-drive` CLI (browser login), deployed to `/var/lib/portunus/skills/proton-drive`. Binary is fleet-provisioned to `/usr/local/bin/proton-drive`.
- `agent-mail/` — 腾讯 Agent Mail wrapper around `@tencent-qqmail/agently-cli` (browser OAuth), deployed to `/var/lib/portunus/skills/agent-mail`. Runners need `agently-cli` on PATH or `npx` (pin in wrapper).
- `baidu-drive/` — 百度网盘 wrapper around the official `bdpan` CLI (paste-code OAuth), deployed to `/var/lib/portunus/skills/baidu-drive`. Binary is fleet-provisioned to `/usr/local/bin/bdpan`.
