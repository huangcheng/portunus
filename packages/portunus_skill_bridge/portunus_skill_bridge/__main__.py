"""CLI: portunus-skill-bridge --manifest PATH | --manifest-env VAR | --manifest-stdin"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from portunus_skill_bridge.manifest import Manifest, load_manifest
from portunus_skill_bridge.server import build_server


def _load_from_args(args: argparse.Namespace) -> Manifest:
    modes = [args.manifest, args.manifest_env, args.manifest_stdin]
    if sum(1 for m in modes if m) != 1:
        raise SystemExit("exactly one of --manifest, --manifest-env, --manifest-stdin is required")
    if args.manifest:
        return load_manifest(args.manifest)
    if args.manifest_env:
        raw = os.environ.get(args.manifest_env, "")
        if not raw.strip():
            raise SystemExit(f"env {args.manifest_env!r} is empty (set by Portunus from catalog)")
        return Manifest.model_validate(json.loads(raw))
    # stdin
    data = json.load(sys.stdin)
    return Manifest.model_validate(data)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="portunus-skill-bridge")
    p.add_argument("--manifest", help="Path to YAML/JSON manifest file")
    p.add_argument(
        "--manifest-env",
        metavar="VAR",
        help="Read JSON manifest from environment variable (DB-fed hot path)",
    )
    p.add_argument(
        "--manifest-stdin",
        action="store_true",
        help="Read JSON manifest from stdin",
    )
    args = p.parse_args(argv)
    try:
        manifest = _load_from_args(args)
    except SystemExit:
        raise
    except Exception as e:
        print(f"manifest error: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    server = build_server(manifest)
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
