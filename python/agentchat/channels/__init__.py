"""Provider-neutral adapter contracts; import adapters from their own packages."""

from agentchat.channels.base import (
    Channel,
    FileDownloadChannel,
    FileUploadChannel,
    MessageReceiver,
    MirroringChannel,
    RichReplyChannel,
    StatusChannel,
    ThreadHistoryChannel,
)

__all__ = [
    "Channel",
    "FileDownloadChannel",
    "FileUploadChannel",
    "MessageReceiver",
    "MirroringChannel",
    "RichReplyChannel",
    "StatusChannel",
    "ThreadHistoryChannel",
]
