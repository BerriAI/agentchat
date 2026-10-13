from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from agentchat.models import Message


class State(Protocol):
    async def claim(self, message_id: str) -> bool: ...

    def lock(self, conversation_id: str) -> AbstractAsyncContextManager[None]: ...

    async def append(self, message: Message) -> None: ...

    async def history(
        self, conversation_id: str, *, limit: int | None = None
    ) -> tuple[Message, ...]: ...
