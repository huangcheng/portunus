"""Child table: tracks live children, builds inventory, kills and reaps them."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol


class ChildHandle(Protocol):
    """Minimal process handle. asyncio.subprocess.Process-like; tests inject fakes."""

    pid: int
    returncode: int | None

    def kill(self) -> None: ...
    def terminate(self) -> None: ...
    async def wait(self) -> int: ...


class ProcessHandle:
    """Wraps an asyncio subprocess started in its own session (so we can signal the group)."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def _signal(self, sig: int) -> None:
        if self._proc.returncode is not None:
            return
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, sig)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    async def wait(self) -> int:
        return await self._proc.wait()


ExitCallback = Callable[[str, int | None], Awaitable[None] | None]


@dataclass
class ChildRecord:
    child_id: str
    server_id: str
    handle: ChildHandle
    watcher: asyncio.Task | None = field(default=None, repr=False)


class ChildTable:
    def __init__(self, on_exit: ExitCallback | None = None) -> None:
        self._children: dict[str, ChildRecord] = {}
        self.on_exit = on_exit

    def __contains__(self, child_id: str) -> bool:
        return child_id in self._children

    def __len__(self) -> int:
        return len(self._children)

    def empty(self) -> bool:
        return not self._children

    def get(self, child_id: str) -> ChildRecord | None:
        return self._children.get(child_id)

    def add(self, child_id: str, handle: ChildHandle, server_id: str = "") -> ChildRecord:
        if child_id in self._children:
            raise KeyError(child_id)
        rec = ChildRecord(child_id=child_id, server_id=server_id, handle=handle)
        self._children[child_id] = rec
        rec.watcher = asyncio.get_running_loop().create_task(self._watch(rec))
        return rec

    async def _watch(self, rec: ChildRecord) -> None:
        try:
            code = await rec.handle.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            code = rec.handle.returncode
        # Remove only the record we watched (id may have been reused after removal).
        if self._children.get(rec.child_id) is rec:
            del self._children[rec.child_id]
            if self.on_exit is not None:
                with contextlib.suppress(Exception):
                    res = self.on_exit(rec.child_id, code)
                    if asyncio.iscoroutine(res):
                        await res

    def inventory(self) -> list[dict]:
        return [
            {"child_id": r.child_id, "server_id": r.server_id, "pid": r.handle.pid}
            for r in self._children.values()
        ]

    async def kill_all_and_reap(self) -> None:
        """SIGKILL every child and wait() until none remain. Only then is the table empty.

        Loops until the table is empty so that children added concurrently are not missed.
        """
        while self._children:
            recs = list(self._children.values())
            for rec in recs:
                with contextlib.suppress(ProcessLookupError):
                    rec.handle.kill()
            await asyncio.gather(*(rec.handle.wait() for rec in recs), return_exceptions=True)
            for rec in recs:
                # The watcher normally removes the record; do it here too so the table is
                # never non-empty after wait() has returned.
                if self._children.get(rec.child_id) is rec:
                    del self._children[rec.child_id]


async def spawn_dev_simple(msg: dict) -> ChildHandle:
    """Dev-only stopgap spawn (Task 7 replaces with policy-validated, setuid spawn).

    Requires an absolute argv[0]; no policy/grammar validation, no uid drop. Starts the
    child in its own session with a scrubbed env and no stdio, so neither the runner's
    token env nor its fds leak.
    """
    from portunus_shared import PortunusError

    argv = msg["argv"]
    if not os.path.isabs(argv[0]):
        raise PortunusError("spawn_rejected", "argv[0] must be an absolute path")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
            close_fds=True,
        )
    except OSError as e:
        raise PortunusError("spawn_failed", f"exec failed: {e.strerror}") from e
    return ProcessHandle(proc)
