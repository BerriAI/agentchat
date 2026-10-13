"""The native coroutine follows Python's await protocol without moving tasks."""
import asyncio
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentchat import AgentChat
from agentchat.state import MemoryState
from tests.support.channels import FakeChannel, incoming


@pytest.mark.asyncio
async def test_await_iterator_need_not_implement_send():
    class Iterator:
        yielded = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self.yielded:
                self.yielded = True
                return None
            raise StopIteration("custom answer")

    class Awaitable:
        def __await__(self):
            return Iterator()

    channel = FakeChannel()
    app = AgentChat(channels=[channel])
    app.on_message(lambda context: Awaitable())
    await channel.send(incoming("custom", "hello"))
    assert channel.replies == ["custom answer"]


@pytest.mark.asyncio
async def test_generator_based_coroutine_is_a_valid_host_handler():
    channel = FakeChannel()
    app = AgentChat(channels=[channel])

    @app.on_message
    @types.coroutine
    def handler(context):
        yield None
        return "generator answer"

    await channel.send(incoming("generator", "hello"))
    assert channel.replies == ["generator answer"]


@pytest.mark.asyncio
async def test_cancellation_of_iterator_without_throw_keeps_cancelled_error():
    started = asyncio.Event()

    class Iterator:
        def __iter__(self):
            return self

        def __next__(self):
            started.set()
            return None

    class Awaitable:
        def __await__(self):
            return Iterator()

    channel = FakeChannel()
    app = AgentChat(channels=[channel])
    app.on_message(lambda context: Awaitable())
    source = incoming("cancel-custom", "hello")
    task = asyncio.create_task(channel.send(source))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await app.state.claim(source.id) is False
    assert channel.replies == []


def test_coroutine_can_be_created_before_transfer_to_another_thread():
    state = MemoryState()
    pending = state.claim("threaded")
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(asyncio.run, pending).result(timeout=2) is True
    assert asyncio.run(state.claim("threaded")) is False


@pytest.mark.asyncio
async def test_negative_context_history_limit_reports_domain_error():
    channel = FakeChannel()
    app = AgentChat(channels=[channel])

    @app.on_message
    async def handler(context):
        with pytest.raises(ValueError, match="between 2 and 100"):
            await context.history(limit=-1)

    await channel.send(incoming("limit", "hello"))


@pytest.mark.asyncio
async def test_close_releases_conversation_lock_and_closes_suspended_handler():
    channel = FakeChannel()
    state = MemoryState()
    app = AgentChat(channels=[channel], state=state)
    cleanup = []

    @app.on_message
    async def handler(context):
        try:
            await asyncio.sleep(10)
        finally:
            cleanup.append("handler closed")

    source = incoming("close-suspended", "hello")
    pending = app._receive(channel, source)
    yielded = pending.send(None)
    assert yielded is not None
    lock = state.lock(source.conversation_id)
    assert lock.locked()
    pending.close()
    pending.close()
    assert cleanup == ["handler closed"]
    assert not lock.locked()
    assert channel.replies == []
    assert await state.claim(source.id) is False


@pytest.mark.asyncio
async def test_subscription_lookup_reads_state_when_awaited():
    from agentchat.channels.slack import MemoryThreadSubscriptions

    subscriptions = MemoryThreadSubscriptions()
    pending = subscriptions.contains("conversation")
    await subscriptions.add("conversation")
    assert await pending is True


@pytest.mark.asyncio
async def test_public_context_and_app_fields_remain_writable():
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agentchat import MessageContext, Sender

    source = incoming("mutable", "hello")
    channel = FakeChannel()
    app = AgentChat(channels=[channel])
    state = MemoryState()
    app.state = state
    context = MessageContext(app, channel, source)
    updated = replace(source, conversation_id="changed", text="updated")
    context.message = updated
    context.sender = Sender("replacement")
    context.conversation.id = "changed"
    reply = await context.reply("answer")
    assert reply.conversation_id == "changed"
    assert context.sender.id == "replacement"
    assert await context.conversation.history() == (reply,)
    custom_conversation = SimpleNamespace(history=AsyncMock(return_value=(updated,)))
    context.conversation = custom_conversation
    assert await context.history() == (updated,)
    replacement = SimpleNamespace(close=AsyncMock())
    channels = [replacement]
    app.channels = channels
    assert app.channels is channels
    await app.close()
    replacement.close.assert_awaited_once()


def test_closing_abandoned_coroutine_releases_opaque_captured_objects():
    import gc
    import weakref
    from dataclasses import replace

    class Payload:
        pass

    payload = Payload()
    reference = weakref.ref(payload)
    state = MemoryState()
    message = replace(incoming("opaque-cycle", "hello"), metadata={"payload": payload})
    pending = state.append(message)
    payload.pending = pending
    del payload, message, state
    assert reference() is not None
    pending.close()
    gc.collect()
    assert reference() is None


@pytest.mark.asyncio
async def test_delivery_handles_receiver_failure_before_await(caplog):
    from agentchat._delivery import DeliveryTasks

    delivery = DeliveryTasks(1, 1)

    def receiver(channel, message):
        raise ValueError("synchronous receiver failure")

    delivery.receiver = receiver
    assert delivery.submit(FakeChannel(), incoming("receiver-failure", "hello"))
    task, = delivery.tasks
    assert await task is None
    assert "fake message handler failed" in caplog.text
    assert "synchronous receiver failure" in caplog.text
    await delivery.close()


@pytest.mark.asyncio
async def test_working_status_respects_public_update_override():
    from unittest.mock import AsyncMock

    from agentchat.activity import WorkingStatus

    updates = []

    class CustomWorkingStatus(WorkingStatus):
        async def update(self, status):
            updates.append(status)
            return True

    setter = AsyncMock()
    async with CustomWorkingStatus(setter, "working"):
        pass
    assert updates == ["working", ""]
    setter.assert_not_called()
