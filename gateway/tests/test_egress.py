"""Catalog egress → preferred_regions soft affinity."""

from __future__ import annotations

from pathlib import Path

import pytest

from portunus_gateway.catalog import (
    DEFAULT_APPS,
    EGRESS_CHOICES,
    normalize_egress,
    preferred_regions_for_egress,
    preferred_regions_for_slug,
)
from portunus_gateway.registry import RegistryError
from portunus_gateway.toml_catalog import parse_recipe


def test_normalize_egress():
    assert normalize_egress(None) == "any"
    assert normalize_egress("Overseas") == "overseas"
    with pytest.raises(RegistryError):
        normalize_egress("eu-west")


def test_preferred_regions_for_egress():
    assert preferred_regions_for_egress("any") == []
    assert preferred_regions_for_egress("overseas") == ["overseas"]
    assert preferred_regions_for_egress("domestic") == ["domestic"]


def test_default_apps_have_valid_egress():
    for app in DEFAULT_APPS:
        assert app["egress"] in EGRESS_CHOICES
        assert preferred_regions_for_slug(app["slug"]) == preferred_regions_for_egress(
            app["egress"]
        )


def test_toml_recipes_declare_egress():
    apps = Path(__file__).resolve().parents[2] / "catalog" / "apps"
    nutstore = parse_recipe(apps / "nutstore.toml")
    assert nutstore.egress == "domestic"
    dropbox = parse_recipe(apps / "dropbox.toml")
    assert dropbox.egress == "overseas"
    weknora = parse_recipe(apps / "weknora.toml")
    assert weknora.egress == "any"


def test_toml_egress_matches_default_apps():
    """Mode A (DEFAULT_APPS) and mode B (TOML) must agree per slug."""
    apps_dir = Path(__file__).resolve().parents[2] / "catalog" / "apps"
    seed = {str(a["slug"]): normalize_egress(a.get("egress")) for a in DEFAULT_APPS}
    for path in sorted(apps_dir.glob("*.toml")):
        recipe = parse_recipe(path)
        if recipe.slug not in seed:
            continue
        assert normalize_egress(recipe.egress) == seed[recipe.slug], recipe.slug
        assert preferred_regions_for_slug(recipe.slug) == preferred_regions_for_egress(
            recipe.egress
        )
