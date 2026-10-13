# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Awaitable, Callable, Sequence

from agentchat._backend import Backend
from agentchat.activity import WorkingStatus
from agentchat.channels.base import Channel
from agentchat.context import MessageContext
from agentchat.models import (
    Attachment,
    DownloadedFile,
    Message,
    RichReply,
    UploadedFile,
    UploadFile,
)
from agentchat.state.base import State

MessageHandler = Callable[[MessageContext], Awaitable[str | None]]

class AgentChat:
    channels: tuple[Channel, ...]
    state: State

    def __init__(
        self,
        *,
        channels: Sequence[Channel],
        state: State | None = ...,
        backend: Backend | None = ...,
    ) -> None: ...
    @property
    def backend(self) -> str: ...
    def on_message(self, handler: MessageHandler) -> MessageHandler: ...
    async def run(self) -> None: ...
    async def close(self) -> None: ...
    async def reply(self, channel: Channel, source: Message, content: str) -> Message: ...
    async def reply_rich(
        self, channel: Channel, source: Message, content: RichReply
    ) -> Message: ...
    async def upload_files(
        self, channel: Channel, source: Message, files: Sequence[UploadFile]
    ) -> tuple[UploadedFile, ...]: ...
    async def set_status(self, channel: Channel, source: Message, status: str) -> bool: ...
    async def download_attachment(
        self, channel: Channel, source: Message, attachment: Attachment, *, max_bytes: int = ...
    ) -> DownloadedFile: ...
    def working(
        self, channel: Channel, source: Message, status: str = ..., *, refresh_interval: float = ...
    ) -> WorkingStatus: ...
    async def mirror(
        self, channel: Channel, source: Message, message: Message, *, origin: str = ...
    ) -> Message: ...
