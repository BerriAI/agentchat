from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from agentchat.models import Message

MessageReceiver = Callable[["Channel", Message], Awaitable[None]]


class Channel(Protocol):
    name: str

    def bind(self, receiver: MessageReceiver) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...

    async def reply(self, source: Message, content: str) -> Message: ...
