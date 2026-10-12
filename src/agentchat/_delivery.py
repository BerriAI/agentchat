"""Bounded in-process dispatch, shared by the gateway and HTTP adapters."""

from __future__ import annotations

import asyncio
import logging

from agentchat.channels.base import Channel, MessageReceiver
from agentchat.models import Message

logger = logging.getLogger(__name__)


class DeliveryTasks:
    def __init__(self, limit: int, shutdown_timeout: float) -> None:
        if limit < 1 or shutdown_timeout < 0:
            raise ValueError("max_pending_events must be positive; shutdown_timeout nonnegative")
        self.receiver: MessageReceiver | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.limit = limit
        self.shutdown_timeout = shutdown_timeout
        self.closed = False

    def check_bound(self) -> None:
        if self.receiver is None:
            raise RuntimeError("Bind a receiver before running the channel")
        if self.closed:
            raise RuntimeError("A closed channel cannot be restarted")

    def check_reply(self) -> None:
        # Accepted handlers may finish their sends while close() drains them.
        if self.closed and asyncio.current_task() not in self.tasks:
            raise RuntimeError("Channel is closed")

    def submit(self, channel: Channel, message: Message) -> bool:
        if self.closed or len(self.tasks) >= self.limit:
            return False
        self.check_bound()
        task = asyncio.create_task(self._deliver(channel, message))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def _deliver(self, channel: Channel, message: Message) -> None:
        assert self.receiver is not None
        try:
            await self.receiver(channel, message)
        except Exception:
            logger.exception("%s message handler failed", channel.name)

    async def close(self) -> None:
        self.closed = True
        # A receiver may itself request shutdown. Never await/cancel the caller.
        tasks = self.tasks - {asyncio.current_task()}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
