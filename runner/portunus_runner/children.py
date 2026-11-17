"""Child table: tracks live children, builds inventory, kills and reaps them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ChildHandle(Protocol):
    """Minimal process handle. asyncio.subprocess.Process-like; tests inject fakes."""

    pid: int
    returncode: int | None

    def kill(self) -> None: ...
    def terminate(self) -> None: ...
    async def wait(self) -> int: ...


class ProcessHandle:
    """Wraps an asyncio subprocess started in its own session (so we can signal the group)."""

    _STDERR_KEEP = 4 * 1024

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid
        self._swept = False
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        if proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Keep the PIPE drained (avoid child deadlock) and retain a short tail for crashes."""
        assert self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(512)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > self._STDERR_KEEP:
                    del self._stderr_tail[: len(self._stderr_tail) - self._STDERR_KEEP]
        except Exception:
            # Leaving stderr undrained with PIPE can deadlock the child; log once.
            log.exception("stderr drain stopped early for child pid=%s", self.pid)
            return

    @property
    def stderr_tail(self) -> str:
        return bytes(self._stderr_tail).decode(errors="replace").strip()

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

    @property
    def proc(self) -> asyncio.subprocess.Process:
        return self._proc

    async def wait(self) -> int:
        code = await self._proc.wait()
        # Leader is reaped: sweep any surviving members of its process group (e.g. npx
        # grandchildren). Kernel keeps the pgid reserved while members live; if none are
        # left this is ESRCH. After this the early return in _signal is safe (group is empty).
        if not self._swept:
            self._swept = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)
        task = self._stderr_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return code


ExitCallback = Callable[[str, int | None], Awaitable[None] | None]


@dataclass
class ChildRecord:
    child_id: str
    server_id: str
    handle: ChildHandle
    watcher: asyncio.Task | None = field(default=None, repr=False)
    rpc: Any = field(default=None, repr=False)  # rpc.ChildRpc once the child was initialised


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
