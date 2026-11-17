#!/usr/bin/env python3
"""Emit fleet vendor-CLI install specs discovered from skill-bridge manifests.

Scans packages/portunus_skill_bridge/manifests/*.yaml (or PORTUNUS_SKILL_BRIDGE_SRC)
for ``cli.provision`` and prints one JSON object per line for provision-vendor-clis.sh.

No hardcoded app list — adding ``cli.provision`` to a manifest is enough for sync.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _manifests_dir() -> Path:
    env = os.environ.get("PORTUNUS_SKILL_BRIDGE_SRC", "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env) / "manifests")
    here = Path(__file__).resolve().parent
    root = here.parent
    candidates.extend(
        [
            root / "packages" / "portunus_skill_bridge" / "manifests",
            Path("/app/packages/portunus_skill_bridge/manifests"),
        ]
    )
    for c in candidates:
        if c.is_dir():
            return c
    raise SystemExit("discover-vendor-clis: no manifests dir found")


def main() -> int:
    # Prefer packaged loader (validates schema); fall back to PyYAML if unavailable.
    root = Path(__file__).resolve().parent.parent
    for extra in (
        root / "packages" / "portunus_skill_bridge",
        Path("/app/packages/portunus_skill_bridge"),
    ):
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    try:
        from portunus_skill_bridge.manifest import load_manifest
    except ImportError:
        load_manifest = None  # type: ignore[assignment]

    mdir = _manifests_dir()
    seen_dest: set[str] = set()
    for path in sorted(mdir.glob("*.yaml")) + sorted(mdir.glob("*.yml")):
        if load_manifest is not None:
            try:
                m = load_manifest(path)
            except Exception as e:  # noqa: BLE001
                print(f"skip {path.name}: {e}", file=sys.stderr)
                continue
            if m.cli is None or m.cli.provision is None:
                continue
            dest = m.cli.command[0]
            prov = m.cli.provision
            binary = prov.binary or Path(dest).name
            tag = prov.tag
            version = tag[1:] if tag.startswith("v") and len(tag) > 1 else tag
            asset = prov.asset.replace("{version}", version).replace("{tag}", tag)
            rec = {
                "name": m.name,
                "dest": dest,
                "kind": prov.kind,
                "repo": prov.repo,
                "tag": tag,
                "asset": asset,
                "binary": binary,
            }
        else:
            try:
                import yaml  # type: ignore
            except ImportError:
                print("discover-vendor-clis: need portunus_skill_bridge or PyYAML", file=sys.stderr)
                return 2
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"skip {path.name}: {e}", file=sys.stderr)
                continue
            if data is None:
                data = {}
            elif not isinstance(data, dict):
                print(f"skip {path.name}: manifest root must be a mapping", file=sys.stderr)
                continue
            cli = data.get("cli")
            if not isinstance(cli, dict):
                continue
            prov = cli.get("provision")
            cmd = cli.get("command") or []
            if not isinstance(prov, dict) or not isinstance(cmd, list) or not cmd:
                continue
            dest = str(cmd[0])
            repo = prov.get("repo")
            tag = prov.get("tag")
            asset_raw = prov.get("asset")
            if not all(isinstance(x, str) and x.strip() for x in (repo, tag, asset_raw)):
                print(f"skip {path.name}: provision needs non-empty repo/tag/asset", file=sys.stderr)
                continue
            tag_s = str(tag).strip()
            version = tag_s[1:] if tag_s.startswith("v") and len(tag_s) > 1 else tag_s
            asset = str(asset_raw).replace("{version}", version).replace("{tag}", tag_s)
            binary = prov.get("binary") or Path(dest).name
            if not isinstance(binary, str) or "/" in binary or "\\" in binary:
                print(f"skip {path.name}: provision.binary must be a basename", file=sys.stderr)
                continue
            rec = {
                "name": data.get("name") or path.stem,
                "dest": dest,
                "kind": prov.get("kind") or "github_release",
                "repo": str(repo).strip(),
                "tag": tag_s,
                "asset": asset,
                "binary": binary,
            }

        if not dest.startswith("/"):
            print(f"skip {path.name}: dest must be absolute", file=sys.stderr)
            continue
        if dest in seen_dest:
            print(f"skip {path.name}: duplicate dest {dest}", file=sys.stderr)
            continue
        seen_dest.add(dest)
        print(json.dumps(rec, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
