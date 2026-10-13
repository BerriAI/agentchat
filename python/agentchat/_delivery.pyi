# Public compatibility signatures; update with the corresponding Rust binding API.
import asyncio

from agentchat.channels.base import Channel, MessageReceiver
from agentchat.models import Message

class DeliveryTasks:
    receiver: MessageReceiver | None
    tasks: set[asyncio.Task[None]]
    limit: int
    shutdown_timeout: float
    closed: bool

    def __init__(self, limit: int, shutdown_timeout: float) -> None: ...
    def check_bound(self) -> None: ...
    def check_reply(self) -> None: ...
    def submit(self, channel: Channel, message: Message) -> bool: ...
    async def close(self) -> None: ...
