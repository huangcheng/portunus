"""JSON CLI backend: spawn vendor CLI (光鸭-style). Auth/session is external."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from portunus_skill_bridge.manifest import Manifest, ToolSpec


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


class JsonCliBackend:
    def __init__(self, manifest: Manifest) -> None:
        if manifest.cli is None:
            raise ValueError("json_cli requires cli config")
        self.manifest = manifest
        self.cli = manifest.cli

    def _identity_args(self) -> list[str]:
        ident = self.cli.identity
        args: list[str] = []
        if ident.agent_name:
            args += ["--agent-name", ident.agent_name]
        if ident.agent_profile_dir:
            args += ["--agent-profile-dir", _expand(ident.agent_profile_dir)]
        elif ident.agent_instance_id:
            args += ["--agent-instance-id", ident.agent_instance_id]
        return args

    @staticmethod
    def _flag_args(arguments: dict[str, Any]) -> list[str]:
        argv: list[str] = []
        for key, val in arguments.items():
            flag = f"--{key.replace('_', '-')}"
            if val is True:
                argv.append(flag)
            elif val is False or val is None:
                continue
            elif isinstance(val, (list, tuple)):
                for item in val:
                    argv += [flag, str(item)]
            else:
                argv += [flag, str(val)]
        return argv

    async def call(self, tool: ToolSpec, arguments: dict[str, Any]) -> Any:
        cwd = _expand(self.cli.cwd) if self.cli.cwd else None
        if cwd and not Path(cwd).is_dir():
            raise RuntimeError(f"cli.cwd does not exist: {cwd}")

        if tool.cli or self.cli.run_wrapper == "raw":
            # Raw subcommand: quark-style `search --keyword …` (no identity unless set).
            sub = tool.cli or tool.name
            argv = [*self.cli.command, sub, *self._identity_args(), *self._flag_args(arguments)]
            return await self._run(argv, cwd=cwd, stdin=None)

        if self.cli.run_wrapper == "run_operation":
            op_type = tool.operation or tool.name
            op = {"id": "op-1", "type": op_type, **arguments}
            payload = {
                "version": 1,
                "request_id": str(uuid.uuid4()),
                "operations": [op],
            }
            argv = [*self.cli.command, "run", *self._identity_args()]
            return await self._run(argv, cwd=cwd, stdin=json.dumps(payload))

        raise RuntimeError(f"unsupported run_wrapper {self.cli.run_wrapper!r}")

    async def _run(
        self, argv: list[str], *, cwd: str | None, stdin: str | None
    ) -> Any:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate(stdin.encode() if stdin is not None else None)
        out = out_b.decode("utf-8", errors="replace").strip()
        err = err_b.decode("utf-8", errors="replace").strip()
        parsed: Any
        try:
            parsed = json.loads(out) if out else None
        except json.JSONDecodeError:
            parsed = {"raw_stdout": out}
        return {
            "exit_code": proc.returncode,
            "result": parsed,
            "stderr": err or None,
        }
