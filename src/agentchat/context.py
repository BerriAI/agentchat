from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from agentchat.channels.base import ThreadHistoryChannel
from agentchat.models import (
    Attachment,
    DownloadedFile,
    Message,
    RichReply,
    UploadedFile,
    UploadFile,
)

if TYPE_CHECKING:
    from agentchat.activity import WorkingStatus
    from agentchat.app import AgentChat
    from agentchat.channels.base import Channel


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
        if not 2 <= limit <= 100:
            raise ValueError("History limit must be between 2 and 100")
        if self.message.thread_id is not None and isinstance(self._channel, ThreadHistoryChannel):
            previous = await self._channel.thread_history(self.message, limit=limit)
            messages = (*previous, self.message)
            if self._app.backend == "rust":
                from agentchat._native import thread_history_indices

                indices = thread_history_indices(len(messages), limit)
                return tuple(messages[index] for index in indices)
            return messages if len(messages) <= limit else messages[:1] + messages[-(limit - 1):]
        return await self.conversation.history(limit=limit)

    async def reply(self, content: str) -> Message:
        return await self._app.reply(self._channel, self.message, content)

    async def reply_rich(self, content: RichReply) -> Message:
        return await self._app.reply_rich(self._channel, self.message, content)

    async def upload_files(self, files: Sequence[UploadFile]) -> tuple[UploadedFile, ...]:
        return await self._app.upload_files(self._channel, self.message, files)

    async def download_attachment(
        self, attachment: Attachment, *, max_bytes: int = 10 * 1024 * 1024,
    ) -> DownloadedFile:
        """Fetch a current-message attachment after the host authorizes the sender."""
        return await self._app.download_attachment(
            self._channel, self.message, attachment, max_bytes=max_bytes,
        )

    async def set_status(self, status: str) -> bool:
        return await self._app.set_status(self._channel, self.message, status)

    def working(
        self, status: str = "is working…", *, refresh_interval: float = 60,
    ) -> WorkingStatus:
        return self._app.working(
            self._channel, self.message, status, refresh_interval=refresh_interval,
        )
