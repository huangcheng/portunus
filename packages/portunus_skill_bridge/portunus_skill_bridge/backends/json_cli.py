"""JSON CLI backend: spawn vendor CLI (光鸭-style). Auth/session is external."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from portunus_skill_bridge.manifest import Manifest, ToolSpec

# ${NAME} and $NAME. Unresolved references must not become a literal HOME or argv.
_ENV_REF = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand(value: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(value))

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) if match.group(1) is not None else match.group(2)
        got = os.environ.get(name)
        return match.group(0) if got is None else got

    return _ENV_REF.sub(repl, expanded)


def _absolute_existing_dir(raw: str, *, label: str) -> str:
    """Expand ``raw`` and require an absolute directory that already exists."""
    expanded = _expand(raw).strip()
    path = Path(expanded)
    if not expanded or _ENV_REF.search(expanded) or not path.is_absolute() or not path.is_dir():
        raise RuntimeError(f"{label} must be an absolute existing directory")
    return expanded


# Vendor CLIs have no manifest timeout of their own. Bound the wait so a stalled
# child cannot hang the tool call, and reap it if the caller is cancelled.
_CALL_TIMEOUT_S = 60.0
_RESERVED_OP_KEYS = frozenset({"id", "type"})


async def _kill_child(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


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
            args += [
                "--agent-profile-dir",
                _absolute_existing_dir(ident.agent_profile_dir, label="agent_profile_dir"),
            ]
        elif ident.agent_instance_id:
            args += ["--agent-instance-id", ident.agent_instance_id]
        return args

    @staticmethod
    def _value_token(val: Any) -> str:
        text = str(val)
        if text.startswith("-"):
            raise RuntimeError(f"cli argument value must not start with '-': {text!r}")
        return text

    @classmethod
    def _flag_args(cls, arguments: dict[str, Any]) -> list[str]:
        argv: list[str] = []
        for key, val in arguments.items():
            flag = f"--{key.replace('_', '-')}"
            if val is True:
                argv.append(flag)
            elif val is False or val is None:
                continue
            elif isinstance(val, (list, tuple)):
                for item in val:
                    argv += [flag, cls._value_token(item)]
            else:
                argv += [flag, cls._value_token(val)]
        return argv

    async def call(self, tool: ToolSpec, arguments: dict[str, Any]) -> Any:
        cwd = _expand(self.cli.cwd) if self.cli.cwd else None
        if cwd and not Path(cwd).is_dir():
            raise RuntimeError(f"cli.cwd does not exist: {cwd}")

        if tool.cli or self.cli.run_wrapper == "raw":
            # Raw subcommand: quark-style `search --keyword …` (no identity unless set).
            # `cli` may be a multi-token prefix (e.g. cubox `folder list`).
            if isinstance(tool.cli, list):
                if not tool.cli or not all(isinstance(p, str) and p for p in tool.cli):
                    raise RuntimeError(f"tool {tool.name!r} cli list must be non-empty strings")
                sub_argv = list(tool.cli)
            else:
                sub_argv = [tool.cli or tool.name]
            argv = [
                *self.cli.command,
                *sub_argv,
                *self._identity_args(),
                *self._flag_args(arguments),
            ]
            return await self._run(argv, cwd=cwd, stdin=None)

        if self.cli.run_wrapper == "run_operation":
            op_type = tool.operation or tool.name
            reserved = _RESERVED_OP_KEYS.intersection(arguments)
            if reserved:
                names = ", ".join(sorted(reserved))
                raise RuntimeError(f"arguments must not override reserved operation keys: {names}")
            op = {"id": "op-1", "type": op_type, **arguments}
            payload = {
                "version": 1,
                "request_id": str(uuid.uuid4()),
                "operations": [op],
            }
            argv = [*self.cli.command, "run", *self._identity_args()]
            return await self._run(argv, cwd=cwd, stdin=json.dumps(payload))

        raise RuntimeError(f"unsupported run_wrapper {self.cli.run_wrapper!r}")

    def _child_env(self) -> dict[str, str]:
        """Env for the vendor CLI — match runner ``skill_exec`` HOME pinning.

        Guangyapan (and similar device_qr skills) resolve credentials under
        ``$HOME/.…`` *and* ``--agent-profile-dir``. skill_exec always sets
        ``HOME=STATE_DIR``; without that the MCP child invents a fresh agent
        under the process home and returns LOGIN_PENDING despite a good login.
        """
        env = dict(os.environ)
        state = env.get("STATE_DIR", "").strip()
        if state:
            env["HOME"] = _absolute_existing_dir(state, label="STATE_DIR")
            return env
        ident = self.cli.identity
        if ident.agent_profile_dir:
            env["HOME"] = _absolute_existing_dir(ident.agent_profile_dir, label="agent_profile_dir")
        return env

    async def _run(
        self, argv: list[str], *, cwd: str | None, stdin: str | None
    ) -> Any:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=self._child_env(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = stdin.encode() if stdin is not None else None
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(payload), _CALL_TIMEOUT_S)
        except asyncio.CancelledError:
            await _kill_child(proc)
            raise
        except TimeoutError:
            await _kill_child(proc)
            raise RuntimeError(f"cli timed out after {_CALL_TIMEOUT_S:g}s") from None
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
