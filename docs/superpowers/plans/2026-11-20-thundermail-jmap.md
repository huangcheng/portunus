# Thundermail JMAP Implementation Plan

> **For agentic workers:** implement task-by-task. Steps use checkbox syntax.

**Goal:** Ship catalog app `thundermail` with app-password Basic auth against Thundermail JMAP via first-party `portunus-jmap://`.

**Architecture:** Mirror `graph_builtin.py`: `JmapBuiltinClient` + `RemoteProxy` scheme dispatch; catalog `http` recipe; dashboard Basic Connect fields for email/app password.

**Tech Stack:** Python httpx, JMAP over HTTPS, Portunus vault Authorization header

**Spec:** `docs/superpowers/specs/2026-11-20-thundermail-jmap-design.md`

## Task 1: JMAP builtin + URL validation

- [ ] `gateway/portunus_gateway/jmap_builtin.py` + tests
- [ ] Wire `RemoteProxy`, `validate_remote_url`

## Task 2: Catalog + Connect UI

- [ ] DEFAULT_APPS + `catalog/apps/thundermail.toml`
- [ ] HttpManagePanel: email + app password → Basic Authorization
- [ ] Icon if official mark available (else initial)

## Task 3: Verify + ship

- [ ] Tests green; commit; push; deploy; ensure_defaults
