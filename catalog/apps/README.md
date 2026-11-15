# Portunus app recipes

TOML Connect recipes (design `2026-11-13-toml-app-catalog-design.md`).

- Filename stem **must** equal `slug` (`[a-z0-9-]{1,64}`).
- Gateway loads this directory at start (`PORTUNUS_APPS_DIR` overrides).
- Keep **in parity** with `gateway/portunus_gateway/catalog.py` `DEFAULT_APPS`
  (transitional SQLite seed / `ensure_defaults`). When adding an app, update
  both the TOML recipe here and `DEFAULT_APPS` (plus icons / skill manifests).
- Validate: `cd gateway && uv run pytest tests/test_toml_catalog.py`
- Regenerate from `DEFAULT_APPS` (checkout only): `python scripts/_gen_catalog_tomls.py`
