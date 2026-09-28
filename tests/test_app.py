import asyncio
from types import MappingProxyType

from agentchat import AgentChat, Message, MessageContext, Sender
from agentchat.channels.base import MessageReceiver
from agentchat.state import MemoryState


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


def test_routes_replies_and_preserves_conversation_history() -> None:
    async def scenario() -> None:
        channel = FakeChannel()
        app = AgentChat(channels=[channel], state=MemoryState())

        @app.on_message
        async def respond(context: MessageContext) -> str:
            history = await context.conversation.history()
            return f"turn {len(history)}: {context.message.text}"

        await channel.send(incoming("message-1", "hello"))
        await channel.send(incoming("message-2", "again"))

        assert channel.replies == ["turn 1: hello", "turn 3: again"]
        history = await app.state.history("fake:conversation")
        assert [message.text for message in history] == [
            "hello",
            "turn 1: hello",
            "again",
            "turn 3: again",
        ]

    asyncio.run(scenario())


def test_deduplicates_incoming_messages() -> None:
    async def scenario() -> None:
        channel = FakeChannel()
        app = AgentChat(channels=[channel], state=MemoryState())

        @app.on_message
        async def respond(context: MessageContext) -> str:
            return context.message.text

        message = incoming("duplicate", "hello")
        await channel.send(message)
        await channel.send(message)

        assert channel.replies == ["hello"]

    asyncio.run(scenario())
