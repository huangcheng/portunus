"""File-backed Connect recipes (`catalog/apps/*.toml`) — design 2026-11-13.

Load/verify at gateway start (and CI). DB holds live servers only; this module is
the recipe SoT for Release A/B.
"""

from __future__ import annotations

import re
import struct
import tomllib
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from .db.models import CATALOG_CONNECTIONS
from .catalog import validate_spec
from .registry import RegistryError

SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
BIN_PREFIX = "/var/lib/portunus/bin/"
SKILLS_PREFIX = "/var/lib/portunus/skills/"
MAX_TOML_BYTES = 512 * 1024
MAX_ARTIFACTS = 32
ENV_SNAPSHOT_MAX = 32 * 1024


class CatalogVerifyError(ValueError):
    """Recipe or catalog directory failed verify/load rules."""


@dataclass
class Artifact:
    id: str
    kind: str  # install_script | tree
    install_as: str
    script: str | None = None
    script_sha256: str | None = None
    arch: str | None = None
    content_sha256: str | None = None
    content_by_arch: dict[str, str] | None = None
    needs_egress: bool = True
    path: str | None = None  # tree packaging hint


@dataclass
class Recipe:
    stem: str
    path: Path
    slug: str
    label: str
    description: str
    connection: str
    popular: bool
    categories: list[str]
    tags: list[str]
    sort_order: int
    spec: dict[str, Any]
    artifacts: list[Artifact] = field(default_factory=list)
    enabled: bool = True
    disable_reasons: list[str] = field(default_factory=list)

    @property
    def needs_egress(self) -> bool:
        return any(a.kind == "install_script" and a.needs_egress for a in self.artifacts)

    def arch_set(self) -> set[str]:
        out: set[str] = set()
        for a in self.artifacts:
            if a.kind != "install_script":
                continue
            if a.content_by_arch:
                out |= set(a.content_by_arch)
            elif a.arch:
                out.add(a.arch)
        return out


@dataclass
class LoadedCatalog:
    recipes: list[Recipe]
    digest: str
    apps_dir: Path


def canonical_install_as(raw: str) -> str:
    """Lexical absolute path: collapse `.`/`..`; no realpath / symlink resolution."""
    if not isinstance(raw, str) or not raw:
        raise CatalogVerifyError("install_as must be a non-empty string")
    if "\x00" in raw:
        raise CatalogVerifyError("install_as must not contain NUL")
    if not raw.startswith("/"):
        raise CatalogVerifyError("install_as must be an absolute path")
    if "//" in raw:
        raise CatalogVerifyError("install_as must not contain empty segments")
    parts: list[str] = []
    for seg in raw.split("/"):
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if not parts:
                raise CatalogVerifyError("install_as escapes past root")
            parts.pop()
            continue
        if seg in (".", "..") or "/" in seg or "\\" in seg:
            raise CatalogVerifyError(f"install_as has illegal segment {seg!r}")
        parts.append(seg)
    return "/" + "/".join(parts)


def apps_digest(apps_dir: Path) -> str:
    """sha256 over length-prefixed stem\\0len_be_u64\\0bytes frames, sorted by stem."""
    frames = bytearray()
    paths = sorted(apps_dir.glob("*.toml"), key=lambda p: p.stem.encode("utf-8"))
    for path in paths:
        raw = path.read_bytes()
        stem = path.stem.encode("utf-8")
        frames.extend(stem)
        frames.append(0)
        frames.extend(struct.pack(">Q", len(raw)))
        frames.extend(raw)
    return sha256(frames).hexdigest()


def _require_str(data: dict[str, Any], key: str) -> str:
    val = data.get(key)
    if not isinstance(val, str) or not val.strip():
        raise CatalogVerifyError(f"{key} must be a non-empty string")
    return val.strip()


def _hex64(val: Any, field: str) -> str:
    if not isinstance(val, str) or not HEX64_RE.match(val):
        raise CatalogVerifyError(f"{field} must be 64 lowercase hex chars")
    return val


def _parse_artifact(raw: dict[str, Any]) -> Artifact:
    if not isinstance(raw, dict):
        raise CatalogVerifyError("artifact must be a table")
    aid = _require_str(raw, "id")
    kind = _require_str(raw, "kind")
    install_as = canonical_install_as(_require_str(raw, "install_as"))
    if kind == "install_script":
        if not install_as.startswith(BIN_PREFIX) or install_as == BIN_PREFIX.rstrip("/"):
            raise CatalogVerifyError(f"install_script install_as must be under {BIN_PREFIX}")
        script = _require_str(raw, "script")
        script_sha256 = _hex64(raw.get("script_sha256"), "script_sha256")
        arch = raw.get("arch")
        content_sha256 = raw.get("content_sha256")
        content_by_arch = raw.get("content_by_arch")
        has_scalar = arch is not None or content_sha256 is not None
        has_map = content_by_arch is not None
        if has_scalar and has_map:
            raise CatalogVerifyError(
                "install_script XOR: use either (arch+content_sha256) or content_by_arch, not both"
            )
        if has_scalar:
            if not isinstance(arch, str) or not arch.startswith("linux/"):
                raise CatalogVerifyError("arch must be linux/<GOARCH>")
            content_sha256 = _hex64(content_sha256, "content_sha256")
            content_by_arch = None
        elif has_map:
            if not isinstance(content_by_arch, dict) or not content_by_arch:
                raise CatalogVerifyError("content_by_arch must be a non-empty table")
            cleaned: dict[str, str] = {}
            for k, v in content_by_arch.items():
                if not isinstance(k, str) or not k.startswith("linux/"):
                    raise CatalogVerifyError("content_by_arch keys must be linux/<GOARCH>")
                cleaned[k] = _hex64(v, f"content_by_arch[{k}]")
            content_by_arch = cleaned
            arch = None
            content_sha256 = None
        else:
            raise CatalogVerifyError(
                "install_script XOR: require (arch+content_sha256) or content_by_arch"
            )
        needs_egress = raw.get("needs_egress", True)
        if not isinstance(needs_egress, bool):
            raise CatalogVerifyError("needs_egress must be a boolean")
        return Artifact(
            id=aid,
            kind=kind,
            install_as=install_as,
            script=script,
            script_sha256=script_sha256,
            arch=arch,
            content_sha256=content_sha256,
            content_by_arch=content_by_arch,
            needs_egress=needs_egress,
        )
    if kind == "tree":
        if not install_as.startswith(SKILLS_PREFIX) or install_as == SKILLS_PREFIX.rstrip("/"):
            raise CatalogVerifyError(f"tree install_as must be under {SKILLS_PREFIX}")
        path_hint = raw.get("path")
        if path_hint is not None and not isinstance(path_hint, str):
            raise CatalogVerifyError("tree path must be a string")
        return Artifact(id=aid, kind=kind, install_as=install_as, path=path_hint)
    raise CatalogVerifyError(f"unknown artifact kind {kind!r}")


def _normalize_secrets(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CatalogVerifyError("spec.secrets must be a list")
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]:
            names.append(item["name"])
        else:
            raise CatalogVerifyError("spec.secrets entries must be names or {name=...}")
    return names


def _normalize_env_defaults(raw: Any, secret_names: set[str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CatalogVerifyError("spec.env_defaults must be a table")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k:
            raise CatalogVerifyError("env_defaults keys must be strings")
        if k.startswith("PORTUNUS_"):
            raise CatalogVerifyError(f"env_defaults key {k!r} must not start with PORTUNUS_")
        if k in secret_names:
            raise CatalogVerifyError(f"env_defaults key {k!r} collides with secrets")
        if not isinstance(v, str):
            raise CatalogVerifyError("env_defaults values must be TOML strings")
        out[k] = v
    encoded = str(out).encode("utf-8")
    if len(encoded) > ENV_SNAPSHOT_MAX:
        raise CatalogVerifyError("env_defaults exceeds 32 KiB")
    return out


def parse_recipe(path: Path, *, manifest_root: Path | None = None) -> Recipe:
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_TOML_BYTES:
        raise CatalogVerifyError(f"{path.name} exceeds 512 KiB")
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CatalogVerifyError(f"{path.name}: invalid TOML: {e}") from e
    if not isinstance(data, dict):
        raise CatalogVerifyError(f"{path.name}: root must be a table")

    stem = path.stem
    slug = _require_str(data, "slug")
    if slug != stem:
        raise CatalogVerifyError(f"{path.name}: stem must equal slug ({stem!r} != {slug!r})")
    if not SLUG_RE.match(slug):
        raise CatalogVerifyError(f"slug {slug!r} must match [a-z0-9-]{{1,64}}")

    connection = _require_str(data, "connection")
    if connection not in CATALOG_CONNECTIONS:
        raise CatalogVerifyError(f"unsupported connection {connection!r}")

    label = _require_str(data, "label")
    description = _require_str(data, "description")
    popular = data.get("popular", False)
    if not isinstance(popular, bool):
        raise CatalogVerifyError("popular must be a boolean")
    categories = data.get("categories", [])
    tags = data.get("tags", [])
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        raise CatalogVerifyError("categories must be a string list")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise CatalogVerifyError("tags must be a string list")
    sort_order = data.get("sort_order", 0)
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise CatalogVerifyError("sort_order must be an integer")

    spec_raw = data.get("spec")
    if not isinstance(spec_raw, dict):
        raise CatalogVerifyError("spec must be a table")

    # TOML skill_bridge: manifest is a filename; expand for validate_spec compatibility.
    spec_for_validate = dict(spec_raw)
    secret_names = _normalize_secrets(spec_raw.get("secrets"))
    env_defaults = _normalize_env_defaults(spec_raw.get("env_defaults"), set(secret_names))

    if connection == "skill_bridge":
        manifest_name = spec_raw.get("manifest")
        if not isinstance(manifest_name, str) or not manifest_name.endswith((".yaml", ".yml")):
            raise CatalogVerifyError("skill_bridge spec.manifest must be a .yaml filename")
        root = manifest_root
        if root is None:
            import portunus_skill_bridge

            root = Path(portunus_skill_bridge.__file__).resolve().parent.parent / "manifests"
        manifest_path = root / Path(manifest_name).name
        if not manifest_path.is_file():
            raise CatalogVerifyError(f"manifest_missing: {manifest_path.name}")
        from portunus_skill_bridge.manifest import load_manifest

        spec_for_validate = {
            "manifest": load_manifest(manifest_path).model_dump(mode="json", exclude_none=True),
            "secrets": [{"name": n, "label": n, "secret": True} for n in secret_names],
        }
        if env_defaults:
            spec_for_validate["env_defaults"] = env_defaults
    elif connection in ("oauth", "http", "webdav", "stdio"):
        # secrets as names are not part of validate_spec for these; strip env_defaults
        # into a side channel after validation.
        pass

    try:
        spec = validate_spec(connection, spec_for_validate)
    except RegistryError as e:
        raise CatalogVerifyError(f"spec invalid: {e}") from e

    if connection != "skill_bridge":
        spec["secrets"] = secret_names
        if env_defaults:
            spec["env_defaults"] = env_defaults
    else:
        spec["manifest_name"] = Path(str(spec_raw["manifest"])).name
        spec["secrets"] = secret_names
        if env_defaults:
            spec["env_defaults"] = env_defaults

    artifacts_raw = data.get("artifacts", [])
    if artifacts_raw is None:
        artifacts_raw = []
    if not isinstance(artifacts_raw, list):
        raise CatalogVerifyError("artifacts must be an array")
    if len(artifacts_raw) > MAX_ARTIFACTS:
        raise CatalogVerifyError(f"at most {MAX_ARTIFACTS} artifacts per recipe")

    artifacts: list[Artifact] = []
    seen_ids: set[str] = set()
    for item in artifacts_raw:
        art = _parse_artifact(item)
        if art.id in seen_ids:
            raise CatalogVerifyError(f"duplicate artifact id {art.id!r}")
        seen_ids.add(art.id)
        artifacts.append(art)

    # Intra-recipe install_as clashes + empty arch intersection
    script_by_path: dict[str, Artifact] = {}
    tree_paths: set[str] = set()
    arch_sets: list[set[str]] = []
    for art in artifacts:
        if art.kind == "tree":
            if art.install_as in tree_paths or art.install_as in script_by_path:
                raise CatalogVerifyError(f"cross-kind or duplicate tree install_as {art.install_as}")
            tree_paths.add(art.install_as)
            continue
        if art.install_as in tree_paths:
            raise CatalogVerifyError(f"cross-kind install_as clash {art.install_as}")
        if art.install_as in script_by_path:
            raise CatalogVerifyError(f"intra-recipe install_as clash {art.install_as}")
        script_by_path[art.install_as] = art
        if art.content_by_arch:
            arch_sets.append(set(art.content_by_arch))
        elif art.arch:
            arch_sets.append({art.arch})
    if len(arch_sets) >= 2:
        inter = set.intersection(*arch_sets)
        if not inter:
            raise CatalogVerifyError("empty arch intersection across install_script artifacts")

    return Recipe(
        stem=stem,
        path=path,
        slug=slug,
        label=label,
        description=description,
        connection=connection,
        popular=popular,
        categories=list(categories),
        tags=list(tags),
        sort_order=sort_order,
        spec=spec,
        artifacts=artifacts,
    )


def load_apps_dir(
    apps_dir: Path,
    *,
    manifest_root: Path | None = None,
) -> LoadedCatalog:
    """Load all *.toml; apply on-disk disable rules (sorted stem, first kept)."""
    if not apps_dir.is_dir():
        raise CatalogVerifyError(f"apps dir missing: {apps_dir}")

    digest = apps_digest(apps_dir)
    recipes: list[Recipe] = []
    for path in sorted(apps_dir.glob("*.toml"), key=lambda p: p.stem):
        try:
            recipes.append(parse_recipe(path, manifest_root=manifest_root))
        except CatalogVerifyError as e:
            recipes.append(
                Recipe(
                    stem=path.stem,
                    path=path,
                    slug=path.stem if SLUG_RE.match(path.stem) else "invalid",
                    label=path.stem,
                    description="",
                    connection="http",
                    popular=False,
                    categories=[],
                    tags=[],
                    sort_order=0,
                    spec={},
                    enabled=False,
                    disable_reasons=[f"parse:{e}"],
                )
            )

    # duplicate slug: first stem wins
    seen_slugs: dict[str, str] = {}
    for r in recipes:
        if not r.enabled:
            continue
        prev = seen_slugs.get(r.slug)
        if prev is not None:
            r.enabled = False
            r.disable_reasons.append("duplicate_slug")
        else:
            seen_slugs[r.slug] = r.stem

    # shared install_as across recipes
    holders: dict[str, tuple[str, str, str, str]] = {}
    # path -> (stem, artifact_id, script_sha256, content_key)
    for r in recipes:
        if not r.enabled:
            continue
        for art in r.artifacts:
            if art.kind != "install_script":
                continue
            rows: list[tuple[str, str]] = []
            if art.content_by_arch:
                for arch, hx in art.content_by_arch.items():
                    rows.append((arch, hx))
            else:
                assert art.arch and art.content_sha256 and art.script_sha256
                rows.append((art.arch, art.content_sha256))
            for arch, hx in rows:
                key = f"{art.install_as}|{arch}"
                sig = (art.script_sha256 or "", hx)
                prev = holders.get(key)
                if prev is None:
                    holders[key] = (r.stem, art.id, sig[0], sig[1])
                elif (prev[2], prev[3]) != sig:
                    r.enabled = False
                    r.disable_reasons.append("shared_install_as_clash")
                    break

    return LoadedCatalog(recipes=recipes, digest=digest, apps_dir=apps_dir)


def packaged_apps_dir() -> Path:
    """Repo-relative default: `<repo>/catalog/apps` when running from a checkout."""
    # gateway/portunus_gateway/toml_catalog.py → repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "catalog" / "apps"


def resolve_apps_dir() -> Path | None:
    """``PORTUNUS_APPS_DIR`` or packaged ``catalog/apps`` if it exists."""
    import os

    override = os.environ.get("PORTUNUS_APPS_DIR")
    if override:
        return Path(override)
    packaged = packaged_apps_dir()
    return packaged if packaged.is_dir() else None


_loaded: LoadedCatalog | None = None


def get_loaded() -> LoadedCatalog | None:
    return _loaded


def set_loaded(catalog: LoadedCatalog | None) -> None:
    global _loaded
    _loaded = catalog


def catalog_mode() -> str:
    import os

    mode = os.environ.get("PORTUNUS_CATALOG_MODE", "A").strip().upper()
    return mode if mode in ("A", "B") else "A"


def recipe_public_view(recipe: Recipe, *, catalog_mode: str = "A", id: str | None = None) -> dict[str, Any]:
    return {
        "id": id,
        "slug": recipe.slug,
        "label": recipe.label,
        "name": recipe.label,
        "description": recipe.description,
        "connection": recipe.connection,
        "popular": recipe.popular,
        "categories": list(recipe.categories),
        "tags": list(recipe.tags),
        "sort_order": recipe.sort_order,
        "spec": dict(recipe.spec),
        "catalog_mode": catalog_mode,
        "needs_egress": recipe.needs_egress,
        "arch_set": sorted(recipe.arch_set()),
        "artifact_count": len(recipe.artifacts),
        "site_pick_ok": None,  # filled by API when runner sites known
    }
