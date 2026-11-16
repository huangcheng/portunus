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
    # None means the flag was omitted. An explicit empty string is still "supplied",
    # so it must not fall through into another mode and then report "exactly one".
    manifest_set = args.manifest is not None
    env_set = args.manifest_env is not None
    stdin_set = bool(args.manifest_stdin)
    if sum((manifest_set, env_set, stdin_set)) != 1:
        raise SystemExit("exactly one of --manifest, --manifest-env, --manifest-stdin is required")
    if manifest_set:
        path = args.manifest.strip()
        if not path:
            raise SystemExit("--manifest requires a non-empty path")
        return load_manifest(path)
    if env_set:
        var = args.manifest_env.strip()
        if not var:
            raise SystemExit("--manifest-env requires a non-empty variable name")
        raw = os.environ.get(var, "")
        if not raw.strip():
            raise SystemExit(f"env {var!r} is empty (set by Portunus from catalog)")
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
