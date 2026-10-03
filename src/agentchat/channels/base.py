from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from agentchat.models import Message, RichReply, UploadedFile, UploadFile

MessageReceiver = Callable[["Channel", Message], Awaitable[None]]


class Channel(Protocol):
    name: str

    def bind(self, receiver: MessageReceiver) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...

    async def reply(self, source: Message, content: str) -> Message: ...


@runtime_checkable
class ThreadHistoryChannel(Protocol):
    """Optional channel capability: authoritative thread messages before the current turn."""

    async def thread_history(self, source: Message, *, limit: int = 50) -> tuple[Message, ...]: ...


@runtime_checkable
class MirroringChannel(Protocol):
    """Optional transport for an authorized user input from another surface."""

    async def mirror(self, source: Message, message: Message, *, origin: str) -> Message: ...


@runtime_checkable
class RichReplyChannel(Protocol):
    """Optional replies with fallback text and provider-compatible rich content."""

    async def reply_rich(self, source: Message, content: RichReply) -> Message: ...


@runtime_checkable
class FileUploadChannel(Protocol):
    """Optional uploads of host-authorized bytes to the source conversation."""

    async def upload_files(
        self, source: Message, files: Sequence[UploadFile],
    ) -> tuple[UploadedFile, ...]: ...


@runtime_checkable
class StatusChannel(Protocol):
    """Optional transient activity indicator, separate from conversation history."""

    async def set_status(self, source: Message, status: str) -> bool: ...
