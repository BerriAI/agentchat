"""Provider-independent expectations for an adapter and its AgentChat integration."""

import asyncio

import pytest

from agentchat import AgentChat, Message

pytestmark = pytest.mark.asyncio


async def test_inbound_messages_have_stable_identity_and_normalized_values(channel_harness):
    received = []

    async def receive(channel, message):
        received.append((channel, message))

    channel_harness.channel.bind(receive)
    await channel_harness.emit("1.0", "hello")
    await channel_harness.emit("1.0", "hello")
    await channel_harness.emit("2.0", "again")

    assert len(received) == 3
    messages = [message for _, message in received]
    assert all(channel is channel_harness.channel for channel, _ in received)
    assert all(isinstance(message, Message) for message in messages)
    assert all(message.channel == channel_harness.channel.name for message in messages)
    assert all(message.sender.id and message.role == "user" for message in messages)
    assert messages[0].id == messages[1].id != messages[2].id
    assert messages[0].conversation_id == messages[2].conversation_id
    assert [message.text for message in messages] == ["hello", "hello", "again"]


async def test_reply_returns_confirmed_message_in_source_conversation(channel_harness):
    received = []

    async def receive(channel, message):
        received.append(message)

    channel_harness.channel.bind(receive)
    await channel_harness.emit("1.0", "hello")
    source = received[0]
    response = await channel_harness.channel.reply(source, "answer")

    assert channel_harness.sent_texts() == ["answer"]
    assert response.id and response.id != source.id
    assert response.conversation_id == source.conversation_id
    assert response.channel == source.channel
    assert response.thread_id == source.thread_id
    assert response.sender.id
    assert response.role == "assistant"
    assert response.text == "answer"


async def test_adapter_works_with_dispatch_history_and_deduplication(channel_harness):
    app = AgentChat(channels=[channel_harness.channel])
    received = []

    @app.on_message
    async def respond(context):
        received.append(context.message)
        return f"reply: {context.message.text}"

    await channel_harness.emit("1.0", "hello")
    await channel_harness.emit("1.0", "hello")

    assert len(received) == 1
    assert channel_harness.sent_texts() == ["reply: hello"]
    history = await app.state.history(received[0].conversation_id)
    assert [(message.role, message.text) for message in history] == [
        ("user", "hello"), ("assistant", "reply: hello"),
    ]


async def test_ambiguous_reply_does_not_record_a_success(channel_harness):
    app = AgentChat(channels=[channel_harness.channel])
    received = []

    @app.on_message
    async def respond(context):
        received.append(context.message)
        return "answer"

    channel_harness.fail_reply()
    with pytest.raises(TimeoutError, match="Delivery outcome is unknown"):
        await channel_harness.emit("1.0", "hello")

    assert await app.state.history(received[0].conversation_id) == (received[0],)


async def test_concurrent_events_in_one_conversation_are_serialized(channel_harness):
    app = AgentChat(channels=[channel_harness.channel])
    started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    @app.on_message
    async def respond(context):
        seen.append(context.message.text)
        if context.message.text == "first":
            started.set()
            await release.wait()
        return context.message.text

    first = asyncio.create_task(channel_harness.emit("1.0", "first"))
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(channel_harness.emit("2.0", "second"))
        await asyncio.sleep(0)
        assert seen == ["first"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        assert seen == ["first", "second"]
        assert channel_harness.sent_texts() == ["first", "second"]
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
