from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from agentchat.models import Message

MessageReceiver = Callable[["Channel", Message], Awaitable[None]]


class Channel(Protocol):
    name: str

    def bind(self, receiver: MessageReceiver) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...

    async def reply(self, source: Message, content: str) -> Message: ...


@runtime_checkable
class ThreadHistoryChannel(Protocol):
    """Optional channel capability: authoritative thread messages before the current turn."""

    async def thread_history(self, source: Message, *, limit: int = 50) -> tuple[Message, ...]: ...
