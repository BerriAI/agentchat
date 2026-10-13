import weakref
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentchat.channels.telegram import Telegram
from agentchat.channels.telegram.transport import TelegramBotClient
from tests.support.telegram import HOOK_URL, SECRET, TOKEN, TelegramClient


@pytest.mark.asyncio
async def test_native_telegram_preserves_subclasses_weakrefs_and_webhook_overrides():
    class CustomTelegram(Telegram):
        def __init__(self, config):
            super().__init__(
                bot_token=config["token"], webhook_url=HOOK_URL, webhook_secret=SECRET,
                client=TelegramClient(),
            )

        async def _receive(self, request):
            return web.Response(text="custom receiver")

    channel = CustomTelegram({"token": TOKEN})
    reference = weakref.ref(channel)
    assert reference() is channel
    assert type(channel) is CustomTelegram
    channel.reply = replacement = AsyncMock(return_value="replacement")
    assert channel.reply is replacement
    try:
        async with TestClient(TestServer(channel.create_app())) as client:
            response = await client.post("/webhooks/telegram")
            assert await response.text() == "custom receiver"
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_native_telegram_client_preserves_subclasses_and_weakrefs():
    class CustomClient(TelegramBotClient):
        def __init__(self, config):
            super().__init__(config["token"])

        async def call(self, method, payload=None, **options):
            return {"method": method}

    client = CustomClient({"token": TOKEN})
    reference = weakref.ref(client)
    assert reference() is client
    try:
        assert await client.call("getMe") == {"method": "getMe"}
        client.call = replacement = AsyncMock(return_value="replacement")
        assert await client.call("getMe") == "replacement"
        assert client.call is replacement
    finally:
        await client.close()
