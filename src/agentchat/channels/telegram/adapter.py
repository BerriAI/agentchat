from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, NamedTuple, Protocol
from urllib.parse import urlsplit

from aiohttp import web

from agentchat._delivery import DeliveryTasks
from agentchat.channels.base import MessageReceiver
from agentchat.channels.telegram.events import normalize
from agentchat.channels.telegram.rendering import text_payload, truncate_caption
from agentchat.channels.telegram.transport import (
    REQUEST_TIMEOUT,
    TelegramBotClient,
    TelegramError,
)
from agentchat.models import (
    Attachment,
    DownloadedFile,
    Message,
    RichReply,
    Sender,
    UploadedFile,
    UploadFile,
)

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30
# The HTTP timeout must outlive Telegram's long-poll hold or every empty poll times out.
POLL_HTTP_MARGIN = 10
ALLOWED_UPDATES = ["message", "callback_query"]
WEBHOOK_PATH = "/webhooks/telegram"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
RETRY_BASE = 1.0
RETRY_MAX = 30.0
BUSY_DELAY = 1.0
CONFIRM_TIMEOUT = 2
ANSWER_TIMEOUT = 5
# Telegram clears a chat action after about five seconds.
TYPING_INTERVAL = 4
TYPING_EXPIRY = 120
BOT_TOKEN_PATTERN = re.compile(r"([0-9]+):[A-Za-z0-9_-]+")
WEBHOOK_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
CALLBACK_ONLY_METADATA = frozenset({"callback_query_id", "callback_data"})


class BotAPIClient(Protocol):
    async def call(
        self,
        method: str,
        payload: dict | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> Any: ...

    async def upload(
        self, *, method: str, fields: dict, file_field: str, filename: str, content: bytes,
    ) -> Any: ...

    async def download_file(self, file_id: str, *, max_bytes: int) -> tuple[str, bytes]: ...

    async def close(self) -> None: ...


class _Destination(NamedTuple):
    chat_id: int
    message_id: int
    thread_id: int | None


def retry_delay(attempt: int, retry_after: int | None) -> float:
    if retry_after:
        return float(retry_after)
    return min(RETRY_BASE * 2**attempt, RETRY_MAX)


def _is_plain_filename(filename: object) -> bool:
    return (
        isinstance(filename, str)
        and bool(filename)
        and len(filename.encode()) <= 255
        and filename not in {".", ".."}
        and not any(char in filename for char in "/\\")
        and not any(ord(char) < 32 for char in filename)
    )


class Telegram:
    """Telegram Bot API: long polling by default, or a secret-checked webhook."""

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        host: str = "127.0.0.1",
        port: int = 3980,
        client: BotAPIClient | None = None,
        max_pending_events: int = 64,
        shutdown_timeout: float = 20,
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise ValueError("Telegram bot_token is required")
        token_match = BOT_TOKEN_PATTERN.fullmatch(bot_token)
        if token_match is None:
            raise ValueError("Telegram bot_token must look like 123456:ABC...")
        if (webhook_url is None) != (webhook_secret is None):
            raise ValueError("Telegram webhook_url and webhook_secret must be given together")
        if webhook_url is not None:
            target = urlsplit(webhook_url)
            if target.scheme != "https" or not target.hostname or target.path != WEBHOOK_PATH:
                raise ValueError(f"Telegram webhook_url must be https and end in {WEBHOOK_PATH}")
        if webhook_secret is not None and not WEBHOOK_SECRET_PATTERN.fullmatch(webhook_secret):
            raise ValueError("Telegram webhook_secret must be 1-256 of A-Z, a-z, 0-9, _ and -")
        self._bot_id = int(token_match[1])
        self._username: str | None = None
        self._first_name: str | None = None
        self._webhook_url, self._webhook_secret = webhook_url, webhook_secret
        self._host, self._port = host, port
        self._client = client or TelegramBotClient(bot_token)
        self._delivery = DeliveryTasks(max_pending_events, shutdown_timeout)
        self._offset: int | None = None
        self._confirmed_offset: int | None = None
        self._worker: asyncio.Task[None] | None = None
        self._runner: web.AppRunner | None = None
        self._typing: dict[tuple[int, int | None], asyncio.Task[None]] = {}
        self._answers: set[asyncio.Task[None]] = set()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_env(cls) -> Telegram:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL") or None
        webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET") or None
        if (webhook_url is None) != (webhook_secret is None):
            raise RuntimeError(
                "TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET are required together"
            )
        return cls(
            bot_token=token,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            host=os.getenv("TELEGRAM_HOST", "127.0.0.1"),
            port=int(os.getenv("TELEGRAM_PORT", "3980")),
        )

    def bind(self, receiver: MessageReceiver) -> None:
        self._delivery.receiver = receiver

    def create_app(self) -> web.Application:
        if self._webhook_secret is None:
            raise RuntimeError("Telegram webhooks need webhook_url and webhook_secret")
        app = web.Application()
        app.router.add_post(WEBHOOK_PATH, self._receive)
        return app

    async def run(self) -> None:
        self._delivery.check_bound()
        if self._worker is not None:
            raise RuntimeError("Telegram channel is already running")
        self._worker = asyncio.create_task(self._serve())
        try:
            await asyncio.wait({self._worker})
        finally:
            await self.close()
        if not self._worker.cancelled():
            self._worker.result()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._delivery.closed = True
            try:
                worker = self._worker
                if worker is not None and worker is not asyncio.current_task():
                    worker.cancel()
                    await asyncio.wait({worker})
                if self._runner:
                    await self._runner.cleanup()
                await self._confirm_offset()
            finally:
                try:
                    await self._delivery.close()
                finally:
                    try:
                        background = {*self._typing.values(), *self._answers}
                        for task in background:
                            task.cancel()
                        await asyncio.gather(*background, return_exceptions=True)
                    finally:
                        await self._client.close()

    async def _identify(self) -> None:
        try:
            me = await self._client.call("getMe")
        except TelegramError as error:
            if error.is_transient:
                raise
            raise TelegramError(
                "Telegram bot authentication failed", error_code=error.error_code,
            ) from None
        username = me.get("username") if isinstance(me, dict) else None
        if not isinstance(username, str) or not username:
            raise RuntimeError("Telegram did not return the bot identity")
        if me.get("id") != self._bot_id:
            raise RuntimeError("Telegram token does not match its bot identity")
        first_name = me.get("first_name")
        self._first_name = first_name if isinstance(first_name, str) else username
        self._username = username

    async def _serve(self) -> None:
        await self._identify()
        if self._webhook_url is None:
            await self._client.call("deleteWebhook", {"drop_pending_updates": False})
            await self._poll()
        else:
            await self._serve_webhook()

    async def _poll(self) -> None:
        attempt = 0
        while True:
            payload: dict[str, object] = {
                "timeout": POLL_TIMEOUT, "allowed_updates": ALLOWED_UPDATES,
            }
            if self._offset is not None:
                payload["offset"] = self._offset
            try:
                updates = await self._client.call(
                    "getUpdates", payload, request_timeout=POLL_TIMEOUT + POLL_HTTP_MARGIN
                )
            except TelegramError as error:
                if not error.is_transient:
                    raise
                delay = retry_delay(attempt, error.retry_after)
                logger.warning("Telegram polling failed (%s); retrying in %.1fs", error, delay)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            attempt = 0
            self._confirmed_offset = self._offset
            if not self._admit(updates if isinstance(updates, list) else []):
                await asyncio.sleep(BUSY_DELAY)

    def _admit(self, updates: list) -> bool:
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int):
                continue
            message = self._normalize(update)
            if message is not None and not self._submit(message):
                return False
            self._offset = update_id + 1
        return True

    def _normalize(self, update: dict) -> Message | None:
        assert self._username is not None
        return normalize(update, bot_id=self._bot_id, bot_username=self._username)

    def _submit(self, message: Message) -> bool:
        if not self._delivery.submit(self, message):
            return False
        callback_id = message.metadata.get("callback_query_id")
        if isinstance(callback_id, str):
            task = asyncio.create_task(self._answer_callback(callback_id))
            self._answers.add(task)
            task.add_done_callback(self._answers.discard)
        return True

    async def _answer_callback(self, callback_id: str) -> None:
        try:
            await self._client.call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id},
                request_timeout=ANSWER_TIMEOUT,
            )
        except Exception as error:
            logger.warning("Telegram callback answer failed (%s)", error)

    async def _confirm_offset(self) -> None:
        # Accepted updates stay unconfirmed until the next getUpdates; a restart would replay them.
        if self._offset is None or self._offset == self._confirmed_offset:
            return
        try:
            await self._client.call(
                "getUpdates",
                {"offset": self._offset, "limit": 1, "timeout": 0},
                request_timeout=CONFIRM_TIMEOUT,
            )
        except TelegramError as error:
            logger.warning("Telegram offset confirmation failed (%s)", error)

    async def _serve_webhook(self) -> None:
        self._runner = web.AppRunner(self.create_app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        # Listen first: Telegram starts sending as soon as setWebhook succeeds.
        await self._client.call(
            "setWebhook",
            {
                "url": self._webhook_url,
                "secret_token": self._webhook_secret,
                "allowed_updates": ALLOWED_UPDATES,
            },
        )
        await asyncio.Event().wait()

    async def _receive(self, request: web.Request) -> web.Response:
        assert self._webhook_secret is not None
        supplied = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), self._webhook_secret.encode()):
            raise web.HTTPUnauthorized(text="Invalid webhook secret")
        try:
            update = json.loads(await request.read())
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid webhook JSON") from None
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            raise web.HTTPBadRequest(text="Invalid webhook update")
        if self._delivery.closed:
            raise web.HTTPServiceUnavailable(text="Telegram receiver is closed")
        if self._username is None:
            try:
                await self._identify()
            except Exception as error:
                logger.warning("Telegram bot identity unavailable (%s)", error)
                raise web.HTTPServiceUnavailable(text="Telegram receiver is starting") from None
        message = self._normalize(update)
        if message is not None and not self._submit(message):
            raise web.HTTPServiceUnavailable(text="Telegram receiver is busy")
        return web.Response(text="OK")

    def _destination(self, source: Message) -> _Destination:
        data = source.metadata
        chat_id, message_id = data.get("chat_id"), data.get("message_id")
        thread = data.get("message_thread_id")
        if (
            source.channel != self.name
            or data.get("bot_id") != self._bot_id
            or not isinstance(chat_id, int)
            or not isinstance(message_id, int)
            or isinstance(chat_id, bool)
            or isinstance(message_id, bool)
            or (thread is not None and (not isinstance(thread, int) or isinstance(thread, bool)))
        ):
            raise ValueError("Source does not belong to this Telegram adapter")
        return _Destination(chat_id, message_id, thread)

    async def _send(
        self, *, source: Message, destination: _Destination, payload: dict, text: str,
    ) -> Message:
        self._delivery.check_reply()
        payload = {
            **payload,
            "chat_id": destination.chat_id,
            "reply_parameters": {
                "message_id": destination.message_id, "allow_sending_without_reply": True,
            },
        }
        if destination.thread_id is not None:
            payload["message_thread_id"] = destination.thread_id
        result = await self._client.call("sendMessage", payload)
        sent = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(sent, int) or isinstance(sent, bool) or sent <= 0:
            raise RuntimeError("Telegram did not return a message receipt")
        metadata = {
            key: value for key, value in source.metadata.items()
            if key not in CALLBACK_ONLY_METADATA
        }
        return Message(
            id=f"telegram:{self._bot_id}:{destination.chat_id}:{sent}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id=str(self._bot_id), display_name=self._first_name),
            text=text,
            role="assistant",
            thread_id=source.thread_id,
            metadata=MappingProxyType({**metadata, "message_id": sent}),
        )

    async def reply(self, source: Message, content: str) -> Message:
        destination = self._destination(source)
        return await self._send(
            source=source, destination=destination, payload=text_payload(content), text=content,
        )

    async def reply_rich(self, source: Message, content: RichReply) -> Message:
        destination = self._destination(source)
        if content.attachments:
            raise ValueError("Telegram rich replies do not support attachments")
        if len(content.blocks) != 1 or not isinstance(content.blocks[0], Mapping):
            raise ValueError("Telegram rich replies need exactly one reply_markup mapping")
        payload = {**text_payload(content.text), "reply_markup": dict(content.blocks[0])}
        return await self._send(
            source=source, destination=destination, payload=payload, text=content.text,
        )

    async def set_status(self, source: Message, status: str) -> bool:
        destination = self._destination(source)
        self._delivery.check_reply()
        key = (destination.chat_id, destination.thread_id)
        if previous := self._typing.pop(key, None):
            previous.cancel()
        if not status:
            return True
        payload: dict[str, object] = {"chat_id": destination.chat_id, "action": "typing"}
        if destination.thread_id is not None:
            payload["message_thread_id"] = destination.thread_id
        await self._client.call("sendChatAction", payload)
        # A concurrent call for the same chat may have stored a task while this one awaited.
        if previous := self._typing.pop(key, None):
            previous.cancel()
        self._typing[key] = asyncio.create_task(self._keep_typing(key, payload))
        return True

    async def _keep_typing(self, key: tuple[int, int | None], payload: dict) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TYPING_EXPIRY
        try:
            while loop.time() + TYPING_INTERVAL < deadline:
                await asyncio.sleep(TYPING_INTERVAL)
                with contextlib.suppress(Exception):
                    await self._client.call(
                        "sendChatAction", payload, request_timeout=TYPING_INTERVAL,
                    )
        finally:
            if self._typing.get(key) is asyncio.current_task():
                del self._typing[key]

    async def upload_files(
        self, source: Message, files: Sequence[UploadFile],
    ) -> tuple[UploadedFile, ...]:
        destination = self._destination(source)
        batch = tuple(files)
        if not batch:
            raise ValueError("Expected at least one file")
        for file in batch:
            if not _is_plain_filename(file.filename):
                raise ValueError("Expected a plain filename")
            if not isinstance(file.content, bytes) or not file.content:
                raise ValueError("Expected nonempty file bytes")
            if file.title is not None and not isinstance(file.title, str):
                raise ValueError("Expected a file title")
        uploaded = []
        for file in batch:
            self._delivery.check_reply()
            fields: dict[str, object] = {"chat_id": destination.chat_id}
            if destination.thread_id is not None:
                fields["message_thread_id"] = destination.thread_id
            if file.title:
                fields["caption"] = truncate_caption(file.title)
            result = await self._client.upload(
                method="sendDocument",
                fields=fields,
                file_field="document",
                filename=file.filename,
                content=file.content,
            )
            document = result.get("document") if isinstance(result, dict) else None
            file_id = document.get("file_id") if isinstance(document, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise RuntimeError("Telegram did not return a document receipt")
            uploaded.append(UploadedFile(id=file_id))
        return tuple(uploaded)

    async def download_attachment(
        self, source: Message, attachment: Attachment, *, max_bytes: int = 10 * 1024 * 1024,
    ) -> DownloadedFile:
        if source.role != "user":
            raise ValueError("Source does not belong to this Telegram adapter")
        self._destination(source)
        if attachment not in source.attachments:
            raise ValueError("Attachment does not belong to the source message")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("Expected a positive attachment byte limit")
        path, content = await self._client.download_file(attachment.id, max_bytes=max_bytes)
        if not content:
            raise ValueError("This Telegram file was empty. Send the attachment again.")
        return DownloadedFile(
            id=attachment.id,
            name=attachment.name or posixpath.basename(path) or "attachment",
            content=content,
            media_type=attachment.media_type,
        )
