# Portunus app recipes

TOML Connect recipes (design `2026-11-13-toml-app-catalog-design.md`).

- Filename stem **must** equal `slug` (`[a-z0-9-]{1,64}`).
- Gateway loads this directory at start (`PORTUNUS_APPS_DIR` overrides).
- Validate: `cd gateway && uv run pytest tests/test_toml_catalog.py`
- Full seed migration from `DEFAULT_APPS` is incremental — not every app is here yet.
