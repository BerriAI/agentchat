"""Foreign transport types and native Telegram client exports."""
from typing import NamedTuple

from agentchat._native import (
    TelegramBotClient as TelegramBotClient,
)
from agentchat._native import (
    telegram_error_init,
    telegram_error_transient,
)

API_ROOT = "https://api.telegram.org"
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_CHUNK_BYTES = 64 * 1024
METHOD_GET_ME = "getMe"
METHOD_GET_UPDATES = "getUpdates"
METHOD_DELETE_WEBHOOK = "deleteWebhook"
METHOD_SET_WEBHOOK = "setWebhook"
METHOD_ANSWER_CALLBACK_QUERY = "answerCallbackQuery"
METHOD_SEND_MESSAGE = "sendMessage"
METHOD_SEND_CHAT_ACTION = "sendChatAction"
METHOD_SEND_DOCUMENT = "sendDocument"
METHOD_GET_FILE = "getFile"


class TelegramFile(NamedTuple):
    path: str
    content: bytes


class TelegramError(RuntimeError):
    def __init__(
        self, description: str, *, error_code: int | None = None, retry_after: int | None = None,
    ) -> None:
        telegram_error_init(self, description, error_code=error_code, retry_after=retry_after)

    @property
    def is_transient(self) -> bool:
        return telegram_error_transient(self)
