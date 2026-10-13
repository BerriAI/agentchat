"""Compatibility exports for the native Telegram adapter."""
from typing import Any, Protocol

from aiohttp import web  # noqa: F401 -- optional dependency import compatibility

from agentchat._native import Telegram as Telegram
from agentchat._native import telegram_retry_delay as retry_delay
from agentchat.channels.telegram.events import (
    METADATA_CALLBACK_DATA,
    METADATA_CALLBACK_QUERY_ID,
    UPDATE_CALLBACK_QUERY,
    UPDATE_MESSAGE,
)
from agentchat.channels.telegram.transport import REQUEST_TIMEOUT, TelegramFile

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_WEBHOOK_URL = "TELEGRAM_WEBHOOK_URL"
ENV_WEBHOOK_SECRET = "TELEGRAM_WEBHOOK_SECRET"
ENV_HOST = "TELEGRAM_HOST"
ENV_PORT = "TELEGRAM_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3980
POLL_TIMEOUT = 30
POLL_REQUEST_TIMEOUT = POLL_TIMEOUT + 10
ALLOWED_UPDATES = [UPDATE_MESSAGE, UPDATE_CALLBACK_QUERY]
WEBHOOK_PATH = "/webhooks/telegram"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
RETRY_BASE = 1.0
RETRY_MAX = 30.0
BUSY_DELAY = 1.0
OFFSET_CONFIRM_TIMEOUT = 2
CALLBACK_ANSWER_TIMEOUT = 5
TYPING_INTERVAL = 4
TYPING_EXPIRY = 120
CHAT_ACTION_TYPING = "typing"
CALLBACK_ONLY_METADATA = frozenset({METADATA_CALLBACK_QUERY_ID, METADATA_CALLBACK_DATA})
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

    async def download_file(self, file_id: str, *, max_bytes: int) -> TelegramFile: ...

    async def close(self) -> None: ...


__all__ = ["BotAPIClient", "Telegram", "retry_delay"]
