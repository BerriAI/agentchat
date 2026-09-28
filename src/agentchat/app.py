from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from agentchat.channels.base import Channel
from agentchat.models import Message, MessageContext
from agentchat.state.base import State
from agentchat.state.memory import MemoryState

MessageHandler = Callable[[MessageContext], Awaitable[str | None]]


class AgentChat:
    def __init__(
        self,
        *,
        channels: Sequence[Channel],
        state: State | None = None,
    ) -> None:
        if not channels:
            raise ValueError("AgentChat requires at least one channel")
        self.channels = tuple(channels)
        self.state = state or MemoryState()
        self._handler: MessageHandler | None = None
        for channel in self.channels:
            channel.bind(self._receive)

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        if self._handler is not None:
            raise RuntimeError("AgentChat supports one message handler")
        self._handler = handler
        return handler

    async def run(self) -> None:
        if self._handler is None:
            raise RuntimeError("Register a message handler with @app.on_message")
        await asyncio.gather(*(channel.run() for channel in self.channels))

    async def close(self) -> None:
        await asyncio.gather(*(channel.close() for channel in self.channels))

    async def reply(self, channel: Channel, source: Message, content: str) -> Message:
        response = await channel.reply(source, content)
        await self.state.append(response)
        return response

    async def _receive(self, channel: Channel, message: Message) -> None:
        if self._handler is None:
            raise RuntimeError("Register a message handler with @app.on_message")
        if not await self.state.claim(message.id):
            return
        async with self.state.lock(message.conversation_id):
            await self.state.append(message)
            context = MessageContext(self, channel, message)
            response = await self._handler(context)
            if response is not None:
                await self.reply(channel, message, response)
