# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Sequence

from agentchat.activity import WorkingStatus
from agentchat.app import AgentChat
from agentchat.channels.base import Channel
from agentchat.models import (
    Attachment,
    DownloadedFile,
    Message,
    RichReply,
    Sender,
    UploadedFile,
    UploadFile,
)

class Conversation:
    id: str

    def __init__(self, app: AgentChat, conversation_id: str) -> None: ...
    async def history(self, *, limit: int | None = ...) -> tuple[Message, ...]: ...

class MessageContext:
    message: Message
    sender: Sender
    conversation: Conversation

    def __init__(self, app: AgentChat, channel: Channel, message: Message) -> None: ...
    async def history(self, *, limit: int = ...) -> tuple[Message, ...]: ...
    async def reply(self, content: str) -> Message: ...
    async def reply_rich(self, content: RichReply) -> Message: ...
    async def upload_files(self, files: Sequence[UploadFile]) -> tuple[UploadedFile, ...]: ...
    async def download_attachment(
        self, attachment: Attachment, *, max_bytes: int = ...
    ) -> DownloadedFile: ...
    async def set_status(self, status: str) -> bool: ...
    def working(self, status: str = ..., *, refresh_interval: float = ...) -> WorkingStatus: ...
