"""TOML app catalog: canonical paths, apps_digest, load/verify (design 2026-11-13)."""

from __future__ import annotations

import struct
from hashlib import sha256
from pathlib import Path

import pytest

from portunus_gateway.toml_catalog import (
    CatalogVerifyError,
    apps_digest,
    canonical_install_as,
    load_apps_dir,
    parse_recipe,
)


def test_canonical_install_as_collapses_dot_dot() -> None:
    assert (
        canonical_install_as("/var/lib/portunus/bin/../bin/rclone")
        == "/var/lib/portunus/bin/rclone"
    )


def test_canonical_install_as_rejects_relative() -> None:
    with pytest.raises(CatalogVerifyError, match="absolute"):
        canonical_install_as("var/lib/portunus/bin/rclone")


def test_canonical_install_as_rejects_empty_segment() -> None:
    with pytest.raises(CatalogVerifyError):
        canonical_install_as("/var/lib//portunus/bin/rclone")


def test_apps_digest_frames_sorted_by_stem(tmp_path: Path) -> None:
    (tmp_path / "b.toml").write_bytes(b"slug = \"b\"\n")
    (tmp_path / "a.toml").write_bytes(b"slug = \"a\"\n")
    # unparsable still hashed
    (tmp_path / "z.toml").write_bytes(b"[[[not valid")

    digest = apps_digest(tmp_path)

    frames = b""
    for stem in ("a", "b", "z"):
        raw = (tmp_path / f"{stem}.toml").read_bytes()
        frames += stem.encode("utf-8") + b"\x00" + struct.pack(">Q", len(raw)) + raw
    assert digest == sha256(frames).hexdigest()


def test_parse_recipe_stem_must_match_slug(tmp_path: Path) -> None:
    path = tmp_path / "quark.toml"
    path.write_text(
        'slug = "other"\n'
        'label = "夸克"\n'
        'description = "x"\n'
        'connection = "skill_bridge"\n'
        "popular = false\n"
        'categories = ["Storage"]\n'
        'tags = ["quark"]\n'
        "sort_order = 76\n"
        "\n"
        "[spec]\n"
        'manifest = "quark.example.yaml"\n'
        "secrets = []\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogVerifyError, match="stem"):
        parse_recipe(path)


def test_parse_recipe_oauth_ok(tmp_path: Path) -> None:
    path = tmp_path / "dropbox.toml"
    path.write_text(
        'slug = "dropbox"\n'
        'label = "Dropbox"\n'
        'description = "Files via Dropbox MCP."\n'
        'connection = "oauth"\n'
        "popular = true\n"
        'categories = ["Storage"]\n'
        'tags = ["oauth"]\n'
        "sort_order = 20\n"
        "\n"
        "[spec]\n"
        'url = "https://mcp.dropbox.com/mcp"\n'
        'authorize_url = "https://www.dropbox.com/oauth2/authorize"\n'
        'token_url = "https://api.dropboxapi.com/oauth2/token"\n'
        'scopes = ["files.content.read"]\n',
        encoding="utf-8",
    )
    recipe = parse_recipe(path)
    assert recipe.slug == "dropbox"
    assert recipe.connection == "oauth"
    assert recipe.artifacts == []
    assert recipe.enabled is True


def test_install_script_xor_requires_arch_or_by_arch(tmp_path: Path) -> None:
    path = tmp_path / "box.toml"
    path.write_text(
        'slug = "box"\n'
        'label = "Box"\n'
        'description = "rclone"\n'
        'connection = "stdio"\n'
        "popular = true\n"
        "categories = []\n"
        "tags = []\n"
        "sort_order = 1\n"
        "\n"
        "[spec]\n"
        'command = "/var/lib/portunus/skills/rclone-shim/shim"\n'
        "args = []\n"
        "\n"
        "[[artifacts]]\n"
        'id = "rclone"\n'
        'kind = "install_script"\n'
        'script = "install-rclone.sh"\n'
        'script_sha256 = "' + ("a" * 64) + '"\n'
        'install_as = "/var/lib/portunus/bin/rclone"\n'
        # missing arch+content_sha256 and content_by_arch
        ,
        encoding="utf-8",
    )
    with pytest.raises(CatalogVerifyError, match="XOR|content_sha256|content_by_arch"):
        parse_recipe(path)


def test_packaged_apps_cover_default_apps() -> None:
    """Every DEFAULT_APPS slug has a parsing TOML recipe under catalog/apps/."""
    from portunus_gateway.catalog import DEFAULT_APPS
    from portunus_gateway.toml_catalog import load_apps_dir, packaged_apps_dir

    apps_dir = packaged_apps_dir()
    loaded = load_apps_dir(apps_dir)
    enabled = {r.slug for r in loaded.recipes if r.enabled}
    want = {a["slug"] for a in DEFAULT_APPS}
    assert want <= enabled, f"missing TOML recipes: {sorted(want - enabled)}"
    disabled = [r for r in loaded.recipes if not r.enabled]
    assert not disabled, [(r.slug, r.disable_reasons) for r in disabled]


def test_packaged_oauth_specs_match_default_apps() -> None:
    from portunus_gateway.catalog import DEFAULT_APPS, validate_spec
    from portunus_gateway.toml_catalog import load_apps_dir, packaged_apps_dir

    loaded = load_apps_dir(packaged_apps_dir())
    recipes = {r.slug: r for r in loaded.recipes}
    for seed in DEFAULT_APPS:
        if seed["connection"] == "oauth":
            recipe_spec = dict(recipes[seed["slug"]].spec)
            if recipe_spec.get("secrets") == []:
                recipe_spec.pop("secrets")
            assert recipe_spec == validate_spec("oauth", seed["spec"])


def test_skill_bridge_toml_secrets_are_objects() -> None:
    """Connect UI needs [{name,label,secret}] — not bare env-name strings."""
    from portunus_gateway.toml_catalog import load_apps_dir, packaged_apps_dir, recipe_public_view

    loaded = load_apps_dir(packaged_apps_dir())
    ima = next(r for r in loaded.recipes if r.slug == "ima")
    secrets = ima.spec.get("secrets")
    assert isinstance(secrets, list) and secrets
    assert all(isinstance(s, dict) and s.get("name") for s in secrets)
    by_name = {s["name"]: s for s in secrets}
    assert set(by_name) == {"IMA_OPENAPI_CLIENTID", "IMA_OPENAPI_APIKEY"}
    assert by_name["IMA_OPENAPI_CLIENTID"]["label"] == "IMA Client ID"
    assert by_name["IMA_OPENAPI_APIKEY"]["label"] == "IMA API Key"
    view = recipe_public_view(ima, catalog_mode="A", id="ima")
    assert {s["name"] for s in view["spec"]["secrets"]} == set(by_name)


def test_load_apps_dir_shared_tree_install_as_clash(tmp_path: Path) -> None:
    def write(slug: str, path_hint: str) -> None:
        (tmp_path / f"{slug}.toml").write_text(
            f'slug = "{slug}"\n'
            'label = "L"\n'
            'description = "d"\n'
            'connection = "http"\n'
            "popular = false\n"
            "categories = []\n"
            "tags = []\n"
            "sort_order = 1\n"
            "\n"
            "[spec]\n"
            'url = "https://example.com/mcp"\n'
            "\n"
            "[[artifacts]]\n"
            'id = "skill"\n'
            'kind = "tree"\n'
            'install_as = "/var/lib/portunus/skills/shared"\n'
            f'path = "{path_hint}"\n',
            encoding="utf-8",
        )

    write("alpha", "third_party/a")
    write("beta", "third_party/b")
    write("gamma", "third_party/a")
    loaded = load_apps_dir(tmp_path)
    by_stem = {r.stem: r for r in loaded.recipes}
    assert by_stem["alpha"].enabled is True
    assert by_stem["beta"].enabled is False
    assert "shared_install_as_clash" in by_stem["beta"].disable_reasons
    assert by_stem["gamma"].enabled is True


def test_load_apps_dir_shared_install_as_clash(tmp_path: Path) -> None:
    hx_a = "a" * 64
    hx_b = "b" * 64
    script_h = "c" * 64
    for slug, content_hx in (("alpha", hx_a), ("beta", hx_b)):
        (tmp_path / f"{slug}.toml").write_text(
            f'slug = "{slug}"\n'
            'label = "L"\n'
            'description = "d"\n'
            'connection = "http"\n'
            "popular = false\n"
            "categories = []\n"
            "tags = []\n"
            "sort_order = 1\n"
            "\n"
            "[spec]\n"
            'url = "https://example.com/mcp"\n'
            "\n"
            "[[artifacts]]\n"
            'id = "rclone"\n'
            'kind = "install_script"\n'
            'script = "install-rclone.sh"\n'
            f'script_sha256 = "{script_h}"\n'
            'install_as = "/var/lib/portunus/bin/rclone"\n'
            'arch = "linux/amd64"\n'
            f'content_sha256 = "{content_hx}"\n',
            encoding="utf-8",
        )
    loaded = load_apps_dir(tmp_path)
    by_stem = {r.stem: r for r in loaded.recipes}
    assert by_stem["alpha"].enabled is True
    assert by_stem["beta"].enabled is False
    assert "shared_install_as_clash" in by_stem["beta"].disable_reasons


def _recipe_with_scripts(tmp_path: Path, slug: str, scripts: list[tuple[str, str, str]]) -> None:
    script_h = "e" * 64
    arts = []
    for aid, install_as, hx in scripts:
        arts.append(
            "[[artifacts]]\n"
            f'id = "{aid}"\n'
            'kind = "install_script"\n'
            'script = "install.sh"\n'
            f'script_sha256 = "{script_h}"\n'
            f'install_as = "{install_as}"\n'
            'arch = "linux/amd64"\n'
            f'content_sha256 = "{hx}"\n'
        )
    (tmp_path / f"{slug}.toml").write_text(
        f'slug = "{slug}"\n'
        'label = "L"\n'
        'description = "d"\n'
        'connection = "http"\n'
        "popular = false\n"
        "categories = []\n"
        "tags = []\n"
        "sort_order = 1\n"
        "\n"
        "[spec]\n"
        'url = "https://example.com/mcp"\n'
        "\n" + "\n".join(arts),
        encoding="utf-8",
    )


def test_clash_does_not_publish_later_artifacts_of_disabled_recipe(tmp_path: Path) -> None:
    rclone = "/var/lib/portunus/bin/rclone"
    other = "/var/lib/portunus/bin/other"
    _recipe_with_scripts(tmp_path, "alpha", [("rclone", rclone, "a" * 64)])
    _recipe_with_scripts(
        tmp_path,
        "beta",
        [("rclone", rclone, "b" * 64), ("other", other, "c" * 64)],
    )
    _recipe_with_scripts(tmp_path, "gamma", [("other", other, "d" * 64)])
    loaded = load_apps_dir(tmp_path)
    by_stem = {r.stem: r for r in loaded.recipes}
    assert by_stem["alpha"].enabled is True
    assert by_stem["beta"].enabled is False
    assert "shared_install_as_clash" in by_stem["beta"].disable_reasons
    assert by_stem["gamma"].enabled is True


def test_env_defaults_strips_keys_and_rejects_reserved_or_blank(tmp_path: Path) -> None:
    def write(slug: str, defaults: str, secrets: str = "") -> Path:
        path = tmp_path / f"{slug}.toml"
        path.write_text(
            f'slug = "{slug}"\n'
            'label = "L"\n'
            'description = "d"\n'
            'connection = "stdio"\n'
            "popular = false\n"
            "categories = []\n"
            "tags = []\n"
            "sort_order = 1\n"
            "\n"
            "[spec]\n"
            'command = "/bin/shim"\n'
            "args = []\n"
            f"{secrets}"
            "\n"
            "[spec.env_defaults]\n"
            f"{defaults}\n",
            encoding="utf-8",
        )
        return path

    recipe = parse_recipe(write("shim", '" SKILL_ROOT " = "/opt/skill"'))
    assert recipe.spec["env_defaults"] == {"SKILL_ROOT": "/opt/skill"}
    with pytest.raises(CatalogVerifyError, match="PORTUNUS_"):
        parse_recipe(write("reserved", '" PORTUNUS_HOME" = "x"'))
    with pytest.raises(CatalogVerifyError, match="non-empty"):
        parse_recipe(write("blank", '"   " = "x"'))
    with pytest.raises(CatalogVerifyError, match="collides with secrets"):
        parse_recipe(write("collide", '" TOKEN " = "x"', 'secrets = ["TOKEN"]\n'))
