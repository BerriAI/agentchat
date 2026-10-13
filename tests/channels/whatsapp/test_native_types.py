import weakref
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentchat.channels.whatsapp import WhatsApp


@pytest.mark.asyncio
async def test_native_whatsapp_preserves_subclasses_weakrefs_and_webhook_overrides():
    class CustomWhatsApp(WhatsApp):
        def __init__(self, config):
            super().__init__(
                phone_number_id=config["phone"], access_token="token", app_secret="secret",
                verify_token="verify",
            )

        async def _receive(self, request):
            return web.Response(text="custom receiver")

    channel = CustomWhatsApp({"phone": "123"})
    reference = weakref.ref(channel)
    assert reference() is channel
    assert type(channel) is CustomWhatsApp
    channel.reply = replacement = AsyncMock(return_value="replacement")
    assert channel.reply is replacement
    try:
        async with TestClient(TestServer(channel.create_app())) as client:
            response = await client.post("/webhooks/whatsapp")
            assert await response.text() == "custom receiver"
    finally:
        await channel.close()
