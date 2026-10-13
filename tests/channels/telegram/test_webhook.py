import asyncio
import json
import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentchat.channels.telegram import Telegram
from agentchat.channels.telegram.adapter import SECRET_HEADER, WEBHOOK_PATH
from agentchat.channels.telegram.transport import TelegramBotClient
from tests.support.telegram import (
    GOOD,
    HOOK_URL,
    SECRET,
    TOKEN,
    FakeBotAPI,
    TelegramClient,
    body,
    callback_update,
    group_update,
    hooked,
    nothing,
    until,
)


@pytest.mark.asyncio
async def test_webhook_statuses():
    client = TelegramClient()
    channel = hooked(client, max_pending_events=1)
    release = asyncio.Event()

    async def handler(channel, message):
        await release.wait()

    channel.bind(handler)
    try:
        async with TestClient(TestServer(channel.create_app())) as http:
            assert (await http.post(WEBHOOK_PATH, data=body(1))).status == 401
            wrong = {**GOOD, SECRET_HEADER: "nope"}
            assert (await http.post(WEBHOOK_PATH, data=body(1), headers=wrong)).status == 401
            assert (await http.post(WEBHOOK_PATH, data=b"{not json", headers=GOOD)).status == 400
            assert (await http.post(WEBHOOK_PATH, data=b"[1]", headers=GOOD)).status == 400
            assert (await http.post(WEBHOOK_PATH, data=body(1), headers=GOOD)).status == 200
            assert client.method_calls("getMe") == [None]
            unaddressed = json.dumps(group_update("chatter"))
            assert (await http.post(WEBHOOK_PATH, data=unaddressed, headers=GOOD)).status == 200
            assert (await http.post(WEBHOOK_PATH, data=body(2), headers=GOOD)).status == 503
            release.set()
            await until(lambda: not channel._delivery.tasks)
            assert (await http.post(WEBHOOK_PATH, data=body(2), headers=GOOD)).status == 200
            await channel.close()
            assert (await http.post(WEBHOOK_PATH, data=body(3), headers=GOOD)).status == 503
            assert client.method_calls("getMe") == [None]
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_webhook_returns_503_until_the_bot_identity_is_known():
    async with FakeBotAPI(TOKEN) as api:
        api.failures["getMe"] = [(502, None)]
        client = TelegramBotClient(TOKEN, api_root=api.root)
        channel = Telegram(bot_token=TOKEN, client=client, webhook_url=HOOK_URL,
                           webhook_secret=SECRET)
        channel.bind(nothing)
        try:
            async with TestClient(TestServer(channel.create_app())) as http:
                assert (await http.post(WEBHOOK_PATH, data=body(1), headers=GOOD)).status == 503
                assert (await http.post(WEBHOOK_PATH, data=body(1), headers=GOOD)).status == 200
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_webhook_callback_tap_is_answered():
    client = TelegramClient()
    channel = hooked(client)
    seen = []

    async def handler(channel, message):
        seen.append(message.text)

    channel.bind(handler)
    try:
        async with TestClient(TestServer(channel.create_app())) as http:
            update = json.dumps(callback_update())
            assert (await http.post(WEBHOOK_PATH, data=update, headers=GOOD)).status == 200
            await until(lambda: client.method_calls("answerCallbackQuery"))
            assert seen == ["Yes please"]
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_webhook_run_listens_before_setting_the_hook_and_leaves_it_set():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    seen = []

    async def handler(channel, message):
        seen.append(message.text)

    async with FakeBotAPI(TOKEN) as api:
        client = TelegramBotClient(TOKEN, api_root=api.root)
        channel = Telegram(bot_token=TOKEN, client=client, webhook_url=HOOK_URL,
                           webhook_secret=SECRET, port=port)
        channel.bind(handler)
        running = asyncio.create_task(channel.run())
        await until(lambda: api.webhook is not None)
        assert api.webhook == {"url": HOOK_URL, "secret_token": SECRET,
                               "allowed_updates": ["message", "callback_query"]}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{port}{WEBHOOK_PATH}",
                                    data=body(5), headers=GOOD) as response:
                assert response.status == 200
        await until(lambda: seen == ["hello"])
        await channel.close()
        assert await asyncio.wait_for(running, 1) is None
        assert channel._client.is_closed
        assert api.webhook is not None
        assert not api.method_calls("deleteWebhook")
