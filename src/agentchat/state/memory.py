from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import MappingProxyType

from agentchat._backend import Backend, resolve_backend
from agentchat.models import Message


class MemoryState:
    def __init__(self, *, backend: Backend | None = None) -> None:
        self._native_store = None
        if resolve_backend(backend) == "rust":
            from agentchat._native import NativeMemoryStore

            self._native_store = NativeMemoryStore()
        self._claimed_message_ids: set[str] = set()
        self._messages: dict[str, tuple[Message, ...]] = {}
        self._conversation_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._state_lock = asyncio.Lock()

    async def claim(self, message_id: str) -> bool:
        async with self._state_lock:
            if self._native_store is not None:
                return self._native_store.claim(message_id)
            if message_id in self._claimed_message_ids:
                return False
            self._claimed_message_ids.add(message_id)
            return True

    @asynccontextmanager
    async def lock(self, conversation_id: str) -> AsyncIterator[None]:
        async with self._conversation_locks[conversation_id]:
            yield

    async def append(self, message: Message) -> None:
        if self._native_store is not None:
            self._native_store.append(message.conversation_id, message)
            return
        current_messages = self._messages.get(message.conversation_id, ())
        self._messages[message.conversation_id] = (*current_messages, message)

    async def history(
        self, conversation_id: str, *, limit: int | None = None
    ) -> tuple[Message, ...]:
        messages = self.messages.get(conversation_id, ())
        return messages if limit is None else messages[-limit:]

    @property
    def messages(self) -> MappingProxyType[str, tuple[Message, ...]]:
        values = self._native_store.messages if self._native_store is not None else self._messages
        return MappingProxyType(values)
