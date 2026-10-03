from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agentchat.activity import WorkingStatus
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


@dataclass(frozen=True, slots=True)
class RichReply:
    """Fallback text and explicitly provider-compatible rich content."""

    text: str
    blocks: tuple[Mapping[str, object], ...] = ()
    attachments: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class UploadFile:
    """File bytes selected and authorized by the host application."""

    filename: str
    content: bytes
    title: str | None = None


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """Confirmed provider file identity; not a conversation message."""

    id: str
    permalink: str | None = None


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

    async def reply_rich(self, content: RichReply) -> Message:
        return await self._app.reply_rich(self._channel, self.message, content)

    async def upload_files(self, files: Sequence[UploadFile]) -> tuple[UploadedFile, ...]:
        return await self._app.upload_files(self._channel, self.message, files)

    async def set_status(self, status: str) -> bool:
        return await self._app.set_status(self._channel, self.message, status)

    def working(
        self, status: str = "is working…", *, refresh_interval: float = 60,
    ) -> WorkingStatus:
        return self._app.working(
            self._channel, self.message, status, refresh_interval=refresh_interval,
        )
