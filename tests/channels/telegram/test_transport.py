import asyncio
import logging
import socket

import pytest

from agentchat.channels.telegram import transport
from agentchat.channels.telegram.transport import TelegramBotClient, TelegramError
from tests.support.telegram import (
    TOKEN,
    FakeBotAPI,
    fast_retries,
    nothing,
    served,
    until,
)


@pytest.mark.asyncio
async def test_long_poll_outlives_the_default_request_timeout(monkeypatch):
    monkeypatch.setattr(transport, "REQUEST_TIMEOUT", 0.2)
    async with FakeBotAPI(TOKEN) as api:
        client = TelegramBotClient(TOKEN, api_root=api.root)
        try:
            assert await client.call("getUpdates", {"timeout": 1}, request_timeout=3) == []
            with pytest.raises(TelegramError, match="uncertain"):
                await client.call("getUpdates", {"timeout": 1}, request_timeout=0.2)
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_no_error_message_contains_token(monkeypatch, caplog):
    fast_retries(monkeypatch)
    caplog.set_level(logging.DEBUG)
    errors = []
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_root = f"http://127.0.0.1:{sock.getsockname()[1]}"
    async with FakeBotAPI(TOKEN) as api:
        api.failures["getMe"] = [
            (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
        ]
        api.failures["setWebhook"] = [(502, None)]
        for root, method, request_timeout in [
            (dead_root, "getMe", 30),
            (api.root, "getMe", 30),
            (api.root, "setWebhook", 30),
            (api.root, "getUpdates", 0.05),
            ("not a url", "getMe", 30),
        ]:
            client = TelegramBotClient(TOKEN, api_root=root)
            try:
                payload = {"timeout": 1} if method == "getUpdates" else {}
                with pytest.raises(TelegramError) as caught:
                    await client.call(method, payload, request_timeout=request_timeout)
                errors.append(caught.value)
            finally:
                await client.close()
        client = TelegramBotClient(TOKEN, api_root=dead_root)

        async def located(method, payload=None, **options):
            return {"file_path": "documents/file.pdf"}

        client.call = located
        try:
            with pytest.raises(TelegramError) as caught:
                await client.download_file("doc", max_bytes=10)
            errors.append(caught.value)
        finally:
            await client.close()
        api.failures["getUpdates"] = [(500, {"ok": False, "error_code": 500, "description": "x"})]
        channel = served(api)
        channel.bind(nothing)
        running = asyncio.create_task(channel.run())
        await until(lambda: any("retrying" in r.message for r in caplog.records))
        await channel.close()
        await running
    for error in errors:
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__
        assert TOKEN not in str(error) and TOKEN not in repr(error)
    # The fake server's own access log prints its request path; only our loggers matter.
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name != "aiohttp.access")
    assert TOKEN not in ours and TOKEN.split(":")[1] not in ours
