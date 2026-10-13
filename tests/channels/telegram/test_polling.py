import asyncio
import time

import pytest

from agentchat import AgentChat
from agentchat.channels.telegram.adapter import retry_delay
from agentchat.channels.telegram.transport import TelegramError
from tests.support.telegram import (
    TOKEN,
    FakeBotAPI,
    TelegramClient,
    callback_update,
    faked,
    fast_retries,
    group_update,
    nothing,
    served,
    telegram_update,
    until,
)


@pytest.mark.asyncio
async def test_callback_tap_is_delivered_and_answered():
    client = TelegramClient(batches=[[callback_update(update_id=5)]])
    channel = faked(client)
    seen = []

    async def receive(channel, message):
        seen.append(message.text)

    channel.bind(receive)
    running = asyncio.create_task(channel.run())
    try:
        await until(lambda: client.method_calls("answerCallbackQuery") and client.started.is_set())
        assert seen == ["Yes please"]
        assert client.method_calls("answerCallbackQuery") == [{"callback_query_id": "cb-1"}]
        assert client.method_calls("getUpdates")[1]["offset"] == 6
    finally:
        await channel.close()
        await running


@pytest.mark.asyncio
async def test_close_cancels_inflight_long_poll_promptly():
    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        channel.bind(nothing)
        running = asyncio.create_task(channel.run())
        await asyncio.wait_for(api.poll_started.wait(), 2)
        started = time.monotonic()
        await channel.close()
        assert time.monotonic() - started < 0.5
        assert await asyncio.wait_for(running, 0.5) is None
        assert api.method_calls("deleteWebhook") == [{"drop_pending_updates": False}]
        poll = api.method_calls("getUpdates")[0]
        assert poll == {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        assert channel._client.is_closed


@pytest.mark.asyncio
async def test_real_poll_to_reply_round_trip_through_agentchat():
    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        app = AgentChat(channels=[channel])

        @app.on_message
        async def respond(context):
            return f"**echo** {context.message.text}"

        await api.push(telegram_update(text="hi"))
        running = asyncio.create_task(app.run())
        try:
            await until(lambda: api.method_calls("sendMessage"))
            sent = api.method_calls("sendMessage")[0]
            assert sent["text"] == "echo hi"
            assert sent["entities"] == [{"type": "bold", "offset": 0, "length": 4}]
            assert sent["reply_parameters"] == {"message_id": 100,
                                                "allow_sending_without_reply": True}
            history = await app.state.history("telegram:42:30")
            assert history[-1].id == "telegram:42:30:501"
            assert history[-1].sender.display_name == "Bot"
        finally:
            await app.close()
            await running


@pytest.mark.asyncio
async def test_full_queue_holds_offset_until_update_is_accepted(monkeypatch):
    fast_retries(monkeypatch)
    release = asyncio.Event()
    seen = []

    async def handler(channel, message):
        seen.append(message.text)
        if len(seen) == 1:
            await release.wait()

    async with FakeBotAPI(TOKEN) as api:
        channel = served(api, max_pending_events=1)
        channel.bind(handler)
        await api.push(telegram_update(text="one", message_id=1))
        second = await api.push(telegram_update(text="two", message_id=2))
        running = asyncio.create_task(channel.run())
        await until(lambda: seen == ["one"])
        await until(lambda: api.offsets.count(second) >= 2)
        assert all(o is None or o <= second for o in api.offsets)
        assert seen == ["one"]
        release.set()
        await until(lambda: seen == ["one", "two"])
        await until(lambda: second + 1 in api.offsets)
        await channel.close()
        await running
        offsets = [o for o in api.offsets if o is not None]
        assert offsets == sorted(offsets)


@pytest.mark.asyncio
async def test_unaddressed_updates_advance_the_offset():
    seen = []

    async def handler(channel, message):
        seen.append(message.text)

    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        channel.bind(handler)
        skipped = await api.push(group_update("chatter", message_id=1))
        await api.push(telegram_update(text="direct", message_id=2))
        running = asyncio.create_task(channel.run())
        await until(lambda: seen == ["direct"])
        await until(lambda: skipped + 2 in api.offsets)
        await channel.close()
        await running


@pytest.mark.asyncio
async def test_close_confirms_accepted_offset():
    seen = []

    async def handler(channel, message):
        seen.append(message.text)

    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        channel.bind(handler)
        update = await api.push(telegram_update(text="one"))
        running = asyncio.create_task(channel.run())
        await until(lambda: seen == ["one"])
        await channel.close()
        await running
        assert api.method_calls("getUpdates")[-1] == {
            "offset": update + 1, "limit": 1, "timeout": 0,
        }
        assert api.updates == []


@pytest.mark.asyncio
async def test_conflict_ends_run_and_closes_session():
    async with FakeBotAPI(TOKEN) as api:
        api.failures["getUpdates"] = [
            (409, {"ok": False, "error_code": 409, "description": "Conflict"}),
        ]
        channel = served(api)
        channel.bind(nothing)
        with pytest.raises(TelegramError, match="Conflict") as caught:
            await asyncio.wait_for(channel.run(), 2)
        assert caught.value.error_code == 409
        assert channel._client.is_closed
        assert channel._delivery.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 404])
async def test_invalid_token_is_fatal(code):
    async with FakeBotAPI(TOKEN) as api:
        api.failures["getMe"] = [(code, {"ok": False, "error_code": code, "description": "Bad"})]
        channel = served(api)
        channel.bind(nothing)
        with pytest.raises(TelegramError, match="authentication failed") as caught:
            await asyncio.wait_for(channel.run(), 2)
        assert caught.value.error_code == code
        assert channel._client.is_closed


@pytest.mark.asyncio
async def test_token_for_another_bot_is_rejected():
    async with FakeBotAPI(TOKEN) as api:
        api.bot["id"] = 7
        channel = served(api)
        channel.bind(nothing)
        with pytest.raises(RuntimeError, match="does not match"):
            await asyncio.wait_for(channel.run(), 2)
        assert not api.method_calls("getUpdates")


@pytest.mark.asyncio
async def test_server_error_backs_off_then_continues(monkeypatch, caplog):
    fast_retries(monkeypatch)
    seen = []

    async def handler(channel, message):
        seen.append(message.text)

    async with FakeBotAPI(TOKEN) as api:
        api.failures["getUpdates"] = [
            (500, {"ok": False, "error_code": 500, "description": "Internal"}),
            (502, None),
            (429, {"ok": False, "error_code": 429, "description": "Too Many",
                   "parameters": {"retry_after": 1}}),
        ]
        channel = served(api)
        channel.bind(handler)
        await api.push(telegram_update(text="after errors"))
        running = asyncio.create_task(channel.run())
        started = time.monotonic()
        await until(lambda: seen == ["after errors"])
        assert time.monotonic() - started >= 1
        await channel.close()
        await running
        assert len([r for r in caplog.records if "retrying" in r.message]) == 3


def test_retry_delay_schedule():
    assert [retry_delay(n, None) for n in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    assert retry_delay(0, 45) == 45


@pytest.mark.asyncio
async def test_external_cancel_of_run_cleans_up():
    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        channel.bind(nothing)
        running = asyncio.create_task(channel.run())
        await asyncio.wait_for(api.poll_started.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 0.5)
        assert channel._worker.done()
        assert channel._client.is_closed
        assert channel._delivery.closed


@pytest.mark.asyncio
async def test_close_from_inside_handler():
    finished = asyncio.Event()

    async def handler(channel, message):
        await channel.close()
        finished.set()

    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        channel.bind(handler)
        await api.push(telegram_update(text="stop"))
        running = asyncio.create_task(channel.run())
        await asyncio.wait_for(finished.wait(), 2)
        assert await asyncio.wait_for(running, 1) is None
        assert channel._client.is_closed
        await until(lambda: not channel._delivery.tasks)


@pytest.mark.asyncio
async def test_close_before_run_and_double_run():
    channel = faked()
    channel.bind(nothing)
    await channel.close()
    await channel.close()
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await channel.run()
