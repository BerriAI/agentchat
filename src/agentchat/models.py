from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agentchat.app import AgentChat
    from agentchat.channels.base import Channel

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Sender:
    id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    channel: str
    sender: Sender
    text: str
    role: Role
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    thread_id: str | None = None
    addressed: bool = True


class Conversation:
    def __init__(self, app: AgentChat, conversation_id: str) -> None:
        self._app = app
        self.id = conversation_id

    async def history(self, *, limit: int | None = None) -> tuple[Message, ...]:
        return await self._app.state.history(self.id, limit=limit)


class MessageContext:
    def __init__(self, app: AgentChat, channel: Channel, message: Message) -> None:
        self._app = app
        self._channel = channel
        self.message = message
        self.sender = message.sender
        self.conversation = Conversation(app, message.conversation_id)

    async def history(self, *, limit: int = 30) -> tuple[Message, ...]:
        """Get shared conversation context, including this turn. Call after authorization."""
        from agentchat.channels.base import ThreadHistoryChannel

        if not 2 <= limit <= 100:
            raise ValueError("History limit must be between 2 and 100")
        if self.message.thread_id is not None and isinstance(self._channel, ThreadHistoryChannel):
            previous = await self._channel.thread_history(self.message, limit=limit)
            messages = (*previous, self.message)
            return messages if len(messages) <= limit else messages[:1] + messages[-(limit - 1):]
        return await self.conversation.history(limit=limit)

    async def reply(self, content: str) -> Message:
        return await self._app.reply(self._channel, self.message, content)
