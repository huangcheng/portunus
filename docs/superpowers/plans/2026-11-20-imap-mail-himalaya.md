# Himalaya IMAP/SMTP providers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One shared Himalaya shim+binary on runners; first provider recipe `qqmail-imap`; later providers = TOML + seed only.

**Architecture:** `himalaya-shim` writes a one-account config from `IMAP_URL` / `SMTP_URL` / `MAIL_ADDRESS` / `MAIL_PASSWORD` and execs `himalaya mcp`. Catalog apps share `command = HIMALAYA_SHIM` and differ via `env_defaults` + copy.

**Tech Stack:** Python shim, Himalaya GitHub release, Portunus catalog TOML / `DEFAULT_APPS`

**Spec:** `docs/superpowers/specs/2026-11-20-qqmail-imap-himalaya-design.md`

## Global Constraints

- One himalaya binary under `/var/lib/portunus/skills/himalaya-shim/`
- Shim path allowlisted like rclone-shim
- SASL PLAIN only; one account per Connect
- Keep `qqmail` OAuth/MCP paste unchanged
- New providers: new `catalog/apps/<slug>.toml` + seed row, same command

## Review Focus

- Empty required env → shim non-zero, clear stderr
- Password TOML escaping (`"`, `\`, `#`)
- Missing himalaya binary → clear error; no `MAIL_PASSWORD` in child env
- `ensure_defaults` inserts `qqmail-imap` when command is `HIMALAYA_SHIM`
- Second provider later must not require a second binary

## File map

| Path | Role |
|------|------|
| `deploy/himalaya-shim/shim` | Shared backend |
| `deploy/provision-himalaya-shim.sh` | One binary + shim + policy |
| `catalog/apps/qqmail-imap.toml` | First provider |
| `gateway/portunus_gateway/catalog.py` | `HIMALAYA_SHIM` + seed; packaged-stdio insert includes shim |
| `deploy/runner-policy.json.example` | Allowlist |
| Tests | shim config + seed shape |

---

### Task 1: Shared shim

**Files:** `deploy/himalaya-shim/run`, `himalaya_config.py`; tests

- [x] Config builder + required-env validation + exec himalaya beside shim
- [x] Clear `MAIL_PASSWORD` from child environ before exec
- [x] skill-bridge wrapper (released himalaya has no `mcp` yet)

### Task 2: First provider `qqmail-imap`

**Files:** `catalog/apps/qqmail-imap.toml`; `catalog.py` seed; tests

- [x] `env_defaults` for QQ IMAP/SMTP URLs; Connect asks address + 授权码
- [x] Shared `himalaya.example.yaml` manifest

### Task 3: Fleet provision

**Files:** `provision-himalaya-shim.sh`; `fleet-providers-update.sh`; Dockerfile

- [x] Download `himalaya.<gnu_arch>-linux.tgz`; rsync run + binary
- [x] `{gnu_arch}` support in `provision-vendor-clis.sh`

### Task 4: Adding another provider (checklist)

Copy `qqmail-imap.toml` → new slug; change label, egress, `env_defaults`, hints; append `DEFAULT_APPS` with `HIMALAYA_MANIFEST`. No shim changes.
