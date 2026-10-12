from types import MappingProxyType

from agentchat import Message, Sender
from agentchat.channels.base import MessageReceiver


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.receiver: MessageReceiver | None = None
        self.replies: list[str] = []

    def bind(self, receiver: MessageReceiver) -> None:
        self.receiver = receiver

    async def run(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def reply(self, source: Message, content: str) -> Message:
        self.replies.append(content)
        return Message(
            id=f"reply:{len(self.replies)}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id="agent"),
            text=content,
            role="assistant",
        )

    async def send(self, message: Message) -> None:
        assert self.receiver is not None
        await self.receiver(self, message)


def incoming(message_id: str, text: str) -> Message:
    return Message(
        id=message_id,
        conversation_id="fake:conversation",
        channel="fake",
        sender=Sender(id="user"),
        text=text,
        role="user",
        metadata=MappingProxyType({}),
    )
