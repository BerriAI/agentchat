"""Best-effort activity lifecycle for in-process message handlers."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType

logger = logging.getLogger(__name__)


class WorkingStatus:
    def __init__(
        self, setter: Callable[[str], Awaitable[bool]], status: str,
        *, refresh_interval: float = 60,
    ) -> None:
        if not 0 < refresh_interval <= 90:
            raise ValueError("Refresh interval must be greater than 0 and at most 90 seconds")
        self._setter = setter
        self._status = status
        self._interval = refresh_interval
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def _send(self, status: str) -> bool:
        try:
            async with asyncio.timeout(3):
                return await self._setter(status)
        except Exception as exc:
            # Presentation failure must not swallow an answer or agent failure.
            logger.warning("Working status unavailable: %s", type(exc).__name__)
            return False

    async def update(self, status: str) -> bool:
        """Set a new label and use it for subsequent refreshes."""
        async with self._lock:
            self._status = status
            return await self._send(status)

    async def _refresh(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            async with self._lock:
                await self._send(self._status)

    async def __aenter__(self) -> WorkingStatus:
        if self._task is not None:
            raise RuntimeError("A working status context cannot be entered twice")
        await self.update(self._status)
        self._task = asyncio.create_task(self._refresh())
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.update("")
