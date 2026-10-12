import asyncio

import pytest

from agentchat.channels._delivery import DeliveryTasks
from tests.support.channels import FakeChannel, incoming


@pytest.mark.asyncio
async def test_dispatch_is_bounded_and_close_cancels_unfinished_receivers():
    delivery = DeliveryTasks(1, shutdown_timeout=0)
    channel = FakeChannel()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def receive(_, message):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    delivery.receiver = receive
    assert delivery.submit(channel, incoming("one", "first"))
    assert not delivery.submit(channel, incoming("two", "overflow"))
    await started.wait()
    await delivery.close()
    assert cancelled.is_set()
    assert not delivery.tasks
    assert not delivery.submit(channel, incoming("three", "closed"))


@pytest.mark.asyncio
async def test_close_drains_successful_receivers():
    delivery = DeliveryTasks(2, shutdown_timeout=1)
    seen = []

    async def receive(_, message):
        seen.append(message.text)

    delivery.receiver = receive
    assert delivery.submit(FakeChannel(), incoming("one", "first"))
    assert delivery.submit(FakeChannel(), incoming("two", "second"))
    await delivery.close()
    assert seen == ["first", "second"]
    assert not delivery.tasks


@pytest.mark.asyncio
async def test_handler_failure_is_logged_and_does_not_leak_task(caplog):
    delivery = DeliveryTasks(1, shutdown_timeout=1)

    async def receive(*args):
        raise RuntimeError("handler failed")

    delivery.receiver = receive
    delivery.submit(FakeChannel(), incoming("one", "first"))
    await delivery.close()
    assert "handler failed" in caplog.text
    assert not delivery.tasks


@pytest.mark.asyncio
async def test_receiver_can_request_shutdown_without_waiting_for_itself():
    delivery = DeliveryTasks(1, shutdown_timeout=1)
    finished = asyncio.Event()

    async def receive(*args):
        await delivery.close()
        finished.set()

    delivery.receiver = receive
    delivery.submit(FakeChannel(), incoming("one", "first"))
    await asyncio.wait_for(finished.wait(), timeout=0.5)


def test_unbound_and_invalid_configuration():
    with pytest.raises(ValueError):
        DeliveryTasks(0, 1)
    with pytest.raises(ValueError):
        DeliveryTasks(1, -1)
    with pytest.raises(RuntimeError, match="Bind"):
        DeliveryTasks(1, 1).submit(FakeChannel(), incoming("one", "first"))
