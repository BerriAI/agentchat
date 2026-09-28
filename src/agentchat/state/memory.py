from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import MappingProxyType

from agentchat.models import Message


class MemoryState:
    def __init__(self) -> None:
        self._claimed_message_ids: set[str] = set()
        self._messages: dict[str, tuple[Message, ...]] = {}
        self._conversation_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._state_lock = asyncio.Lock()

    async def claim(self, message_id: str) -> bool:
        async with self._state_lock:
            if message_id in self._claimed_message_ids:
                return False
            self._claimed_message_ids.add(message_id)
            return True

    @asynccontextmanager
    async def lock(self, conversation_id: str) -> AsyncIterator[None]:
        async with self._conversation_locks[conversation_id]:
            yield

    async def append(self, message: Message) -> None:
        current_messages = self._messages.get(message.conversation_id, ())
        self._messages[message.conversation_id] = (*current_messages, message)

    async def history(
        self, conversation_id: str, *, limit: int | None = None
    ) -> tuple[Message, ...]:
        messages = self._messages.get(conversation_id, ())
        return messages if limit is None else messages[-limit:]

    @property
    def messages(self) -> MappingProxyType[str, tuple[Message, ...]]:
        return MappingProxyType(self._messages)
