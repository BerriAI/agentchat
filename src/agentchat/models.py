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

    async def reply(self, content: str) -> Message:
        return await self._app.reply(self._channel, self.message, content)
