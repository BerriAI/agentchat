from collections.abc import Sequence
from dataclasses import FrozenInstanceError

import pytest
from test_app import FakeChannel, incoming

from agentchat import (
    AgentChat,
    Message,
    MessageContext,
    RichReply,
    Sender,
    UploadedFile,
    UploadFile,
)


class RichChannel(FakeChannel):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.rich_calls: list[tuple[Message, RichReply]] = []
        self.upload_calls: list[tuple[Message, Sequence[UploadFile]]] = []
        self.receipts = (UploadedFile("file:confirmed", "https://provider.example/file"),)

    async def reply_rich(self, source: Message, content: RichReply) -> Message:
        self.rich_calls.append((source, content))
        if self.fail:
            raise TimeoutError("Response lost after send")
        return Message(
            id="provider:confirmed", conversation_id=source.conversation_id,
            channel=self.name, sender=Sender("agent"), text=content.text, role="assistant",
        )

    async def upload_files(
        self, source: Message, files: Sequence[UploadFile],
    ) -> tuple[UploadedFile, ...]:
        self.upload_calls.append((source, files))
        if self.fail:
            raise TimeoutError("Response lost after share")
        return self.receipts


@pytest.mark.asyncio
async def test_existing_text_channels_fail_explicitly_for_optional_rich_capabilities():
    channel = FakeChannel()
    app = AgentChat(channels=[channel])
    context = MessageContext(app, channel, incoming("request", "Send a report"))
    with pytest.raises(TypeError, match="does not support rich replies"):
        await context.reply_rich(RichReply("Ready", attachments=({"color": "#5B3FD1"},)))
    with pytest.raises(TypeError, match="does not support file uploads"):
        await context.upload_files((UploadFile("report.txt", b"report"),))
    assert channel.replies == []
    assert await context.conversation.history() == ()
    fallback = await context.reply("Report available in the application")
    assert channel.replies == [fallback.text]
    assert await context.conversation.history() == (fallback,)


@pytest.mark.asyncio
async def test_context_forwards_source_and_content_without_storing_file_bytes():
    channel = RichChannel()
    app = AgentChat(channels=[channel])
    source = incoming("request", "Send a report")
    files = [UploadFile("report.txt", b"private file bytes", "Report")]
    reply = RichReply(
        "Report ready",
        blocks=({"type": "section", "text": {"type": "plain_text", "text": "Report"}},),
        attachments=({"color": "#5B3FD1"},),
    )
    results = []

    @app.on_message
    async def respond(context: MessageContext) -> None:
        receipts = await context.upload_files(files)
        assert receipts is channel.receipts
        # Only the incoming message exists; file receipts are not chat messages.
        assert await context.conversation.history() == (source,)
        results.append(await context.reply_rich(reply))

    await channel.send(source)
    assert channel.upload_calls == [(source, files)]
    assert channel.rich_calls == [(source, reply)]
    assert channel.replies == []
    assert results[0].id == "provider:confirmed"
    assert await app.state.history(source.conversation_id) == (source, results[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reply", "upload"])
async def test_ambiguous_capability_failures_propagate_without_retry_or_history(operation):
    channel = RichChannel(fail=True)
    app = AgentChat(channels=[channel])
    source = incoming("request", "Send a report")
    await app.state.append(source)
    with pytest.raises(TimeoutError, match="Response lost"):
        if operation == "reply":
            await app.reply_rich(channel, source, RichReply("Report ready"))
        else:
            await app.upload_files(channel, source, (UploadFile("report.txt", b"report"),))
    assert len(channel.rich_calls) + len(channel.upload_calls) == 1
    assert await app.state.history(source.conversation_id) == (source,)


@pytest.mark.parametrize(("value", "field", "replacement"), [
    (UploadFile("report.txt", b"report"), "content", b"replacement"),
    (UploadedFile("provider-id"), "id", "different-id"),
    (RichReply("Ready"), "text", "changed"),
])
def test_delivery_descriptors_are_frozen(value, field, replacement):
    with pytest.raises(FrozenInstanceError):
        setattr(value, field, replacement)
