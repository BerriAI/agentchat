import asyncio
from dataclasses import replace

import pytest

from agentchat import RichReply, UploadFile
from agentchat.channels.telegram import Telegram
from agentchat.channels.telegram import adapter as telegram_adapter
from agentchat.channels.telegram.events import normalize
from agentchat.channels.telegram.transport import TelegramError
from tests.support.receipts import Receipt
from tests.support.telegram import (
    HOOK_URL,
    SECRET,
    TOKEN,
    FakeBotAPI,
    TelegramClient,
    callback_update,
    faked,
    read,
    served,
    telegram_update,
    until,
)


@pytest.mark.asyncio
async def test_reply_payload_routes_to_the_source_and_quotes_it():
    client = TelegramClient()
    channel = faked(client)
    source = read(telegram_update(message_thread_id=7, is_topic_message=True))
    try:
        reply = await channel.reply(source, "**done**")
        (_, payload), _ = client.receipt.calls[0]
        assert payload == {
            "chat_id": 30,
            "text": "done",
            "entities": [{"type": "bold", "offset": 0, "length": 4}],
            "link_preview_options": {"is_disabled": True},
            "reply_parameters": {"message_id": 100, "allow_sending_without_reply": True},
            "message_thread_id": 7,
        }
        assert reply.id == "telegram:99:30:101"
        assert reply.conversation_id == source.conversation_id
        assert reply.thread_id == "7"
        assert reply.text == "**done**"
        assert reply.sender.id == "99"
        assert reply.metadata["message_id"] == 101
        await channel.reply(read(telegram_update()), "plain")
        assert "message_thread_id" not in client.receipt.calls[1][0][1]
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_reply_to_a_callback_drops_tap_only_metadata():
    client = TelegramClient()
    channel = faked(client)
    try:
        reply = await channel.reply(read(callback_update()), "ok")
        assert client.receipt.calls[0][0][1]["reply_parameters"]["message_id"] == 55
        assert "callback_query_id" not in reply.metadata
        assert "callback_data" not in reply.metadata
        assert reply.metadata["message_id"] == 101
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [{"bot_id": 98}, {"chat_id": "30"}, {"message_id": True}, {"message_thread_id": "7"}],
)
async def test_reply_rejects_sources_from_another_bot_or_malformed_routes(metadata):
    client = TelegramClient()
    channel = faked(client)
    source = read(telegram_update())
    source = replace(source, metadata={**source.metadata, **metadata})
    try:
        with pytest.raises(ValueError, match="does not belong"):
            await channel.reply(source, "answer")
        assert not client.calls
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_reply_rich_passes_reply_markup_through():
    client = TelegramClient()
    channel = faked(client)
    source = read(telegram_update())
    keyboard = {"inline_keyboard": [[{"text": "Yes", "callback_data": "yes"}]]}
    try:
        reply = await channel.reply_rich(source, RichReply(text="Continue?", blocks=(keyboard,)))
        payload = client.receipt.calls[0][0][1]
        assert payload["reply_markup"] == keyboard
        assert payload["text"] == "Continue?"
        assert reply.text == "Continue?"
        for content in (
            RichReply(text="x"),
            RichReply(text="x", blocks=(keyboard, keyboard)),
            RichReply(text="x", blocks=(keyboard,), attachments=({"a": 1},)),
        ):
            with pytest.raises(ValueError):
                await channel.reply_rich(source, content)
        assert len(client.receipt.calls) == 1
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_typing_keepalive_refreshes_until_cleared(monkeypatch):
    monkeypatch.setattr(telegram_adapter, "TYPING_INTERVAL", 0.01)
    client = TelegramClient()
    channel = faked(client)
    source = read(telegram_update(message_thread_id=7, is_topic_message=True))
    try:
        assert await channel.set_status(source, "thinking") is True
        assert client.method_calls("sendChatAction")[0] == {
            "chat_id": 30, "action": "typing", "message_thread_id": 7,
        }
        await until(lambda: len(client.method_calls("sendChatAction")) >= 3)
        assert await channel.set_status(source, "") is True
        assert not channel._typing
        sent = len(client.method_calls("sendChatAction"))
        await asyncio.sleep(0.05)
        assert len(client.method_calls("sendChatAction")) == sent
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_typing_keepalive_expires_and_swallows_later_failures(monkeypatch):
    monkeypatch.setattr(telegram_adapter, "TYPING_INTERVAL", 0.01)
    monkeypatch.setattr(telegram_adapter, "TYPING_EXPIRY", 0.1)
    client = TelegramClient()
    channel = faked(client)
    source = read(telegram_update())
    try:
        await channel.set_status(source, "thinking")
        task = channel._typing[(30, None)]
        call = client.call

        async def failing(method, payload=None, **options):
            if method == "sendChatAction":
                client.calls.append((method, payload))
                raise TelegramError("Telegram sendChatAction failed: Too Many", error_code=429)
            return await call(method, payload, **options)

        client.call = failing
        await until(lambda: len(client.method_calls("sendChatAction")) >= 3)
        await asyncio.wait_for(task, 1)
        assert not channel._typing
        with pytest.raises(TelegramError):
            await channel.set_status(source, "thinking")
        assert not channel._typing
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_close_cancels_typing_keepalive_before_closing_the_client():
    client = TelegramClient()
    channel = faked(client)
    await channel.set_status(read(telegram_update()), "thinking")
    task = channel._typing[(30, None)]
    await channel.close()
    assert task.cancelled() and client.closed
    with pytest.raises(RuntimeError, match="closed"):
        await channel.set_status(read(telegram_update()), "thinking")


@pytest.mark.asyncio
async def test_upload_sends_documents_to_the_source_chat_and_thread():
    client = TelegramClient()
    channel = faked(client)
    source = read(telegram_update(message_thread_id=7, is_topic_message=True))
    try:
        uploaded = await channel.upload_files(source, [
            UploadFile("a.txt", b"a", title="😀" * 600),
            UploadFile("b.txt", b"b"),
        ])
        assert [item.id for item in uploaded] == ["file-1", "file-2"]
        method, fields, field, filename, content = client.uploads[0]
        assert (method, field, filename, content) == ("sendDocument", "document", "a.txt", b"a")
        assert fields["chat_id"] == 30 and fields["message_thread_id"] == 7
        assert fields["caption"] == "😀" * 512
        assert "caption" not in client.uploads[1][1]
        for files in ([], [UploadFile("../a", b"a")], [UploadFile("a.txt", b"")]):
            with pytest.raises(ValueError):
                await channel.upload_files(source, files)
        assert len(client.uploads) == 2
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_upload_over_http_is_multipart_with_a_document_receipt():
    async with FakeBotAPI(TOKEN) as api:
        channel = served(api)
        source = normalize(telegram_update(), bot_id=42, bot_username="fake_bot")
        try:
            uploaded = await channel.upload_files(source, [UploadFile("a.txt", b"hi", "Report")])
            assert uploaded[0].id == "uploaded-file" and uploaded[0].permalink is None
            payload = api.method_calls("sendDocument")[0]
            assert payload == {"chat_id": "30", "caption": "Report", "document": ("a.txt", b"hi")}
        finally:
            await channel.close()


@pytest.mark.asyncio
async def test_download_is_bounded_and_never_reveals_the_token():
    async with FakeBotAPI(TOKEN) as api:
        api.files = {
            "doc": {"info": {"file_id": "doc", "file_path": "documents/file_1.pdf"},
                    "content": b"x" * 10},
            "photo": {"info": {"file_id": "photo", "file_path": "photos/file_2.jpg",
                               "file_size": 100}, "content": b"y" * 100},
        }
        channel = served(api)
        message = normalize(telegram_update(
            text=None, document={"file_id": "doc", "file_name": "report.pdf"},
        ), bot_id=42, bot_username="fake_bot")
        photo = normalize(telegram_update(
            text=None, photo=[{"file_id": "photo"}],
        ), bot_id=42, bot_username="fake_bot")
        try:
            downloaded = await channel.download_attachment(message, message.attachments[0])
            assert downloaded.content == b"x" * 10 and downloaded.name == "report.pdf"
            downloaded = await channel.download_attachment(photo, photo.attachments[0])
            assert downloaded.name == "file_2.jpg" and downloaded.media_type == "image/jpeg"
            with pytest.raises(ValueError, match="download limit"):
                await channel.download_attachment(photo, photo.attachments[0], max_bytes=50)
            with pytest.raises(ValueError, match="download limit"):
                await channel.download_attachment(message, message.attachments[0], max_bytes=5)
            assert len(api.method_calls("getFile")) == 4
            api.files["doc"]["content"] = None
            with pytest.raises(RuntimeError, match="HTTP 404") as caught:
                await channel.download_attachment(message, message.attachments[0])
            assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
            api.files["doc"]["info"]["file_path"] = "../secret"
            with pytest.raises(ValueError, match="unavailable"):
                await channel.download_attachment(message, message.attachments[0])
            with pytest.raises(ValueError, match="does not belong"):
                await channel.download_attachment(message, photo.attachments[0])
            with pytest.raises(ValueError, match="does not belong"):
                await channel.download_attachment(
                    replace(message, role="assistant"), message.attachments[0],
                )
            with pytest.raises(ValueError, match="byte limit"):
                await channel.download_attachment(message, message.attachments[0], max_bytes=0)
        finally:
            await channel.close()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"bot_token": " "}, "required"),
        ({"bot_token": "fake"}, "123456:ABC"),
        ({"bot_token": "abc:def"}, "123456:ABC"),
        ({"webhook_url": HOOK_URL}, "together"),
        ({"webhook_secret": SECRET}, "together"),
        ({"webhook_url": "http://example.test/webhooks/telegram", "webhook_secret": SECRET},
         "https"),
        ({"webhook_url": "https://example.test/hook", "webhook_secret": SECRET}, "https"),
        ({"webhook_url": HOOK_URL, "webhook_secret": "bad secret"}, "webhook_secret"),
        ({"webhook_url": HOOK_URL, "webhook_secret": "x" * 257}, "webhook_secret"),
    ],
)
def test_constructor_validation(options, error):
    with pytest.raises(ValueError, match=error):
        Telegram(**{"bot_token": "99:fake", **options})


def test_from_env(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN is required"):
        Telegram.from_env()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "99:fake")
    assert Telegram.from_env()._webhook_url is None
    with pytest.raises(RuntimeError, match="webhook_url and webhook_secret"):
        Telegram.from_env().create_app()
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", HOOK_URL)
    with pytest.raises(RuntimeError, match="required together"):
        Telegram.from_env()
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_PORT", "4000")
    channel = Telegram.from_env()
    assert (channel._webhook_url, channel._port, channel._bot_id) == (HOOK_URL, 4000, 99)


@pytest.mark.asyncio
async def test_reply_rejects_a_boolean_message_receipt():
    channel = faked(TelegramClient(Receipt(True)))
    try:
        with pytest.raises(RuntimeError, match="receipt"):
            await channel.reply(read(telegram_update()), "answer")
    finally:
        await channel.close()
