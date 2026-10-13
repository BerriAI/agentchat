# Public compatibility signatures; update with the corresponding Rust binding API.
from typing import Any, NamedTuple

API_ROOT: str
REQUEST_TIMEOUT: int
UPLOAD_TIMEOUT: int
DOWNLOAD_TIMEOUT: int
DOWNLOAD_CHUNK_BYTES: int
METHOD_GET_ME: str
METHOD_GET_UPDATES: str
METHOD_DELETE_WEBHOOK: str
METHOD_SET_WEBHOOK: str
METHOD_ANSWER_CALLBACK_QUERY: str
METHOD_SEND_MESSAGE: str
METHOD_SEND_CHAT_ACTION: str
METHOD_SEND_DOCUMENT: str
METHOD_GET_FILE: str

class TelegramFile(NamedTuple):
    path: str
    content: bytes

class TelegramError(RuntimeError):
    description: str
    error_code: int | None
    retry_after: int | None

    def __init__(
        self, description: str, *, error_code: int | None = ..., retry_after: int | None = ...
    ) -> None: ...
    @property
    def is_transient(self) -> bool: ...

class TelegramBotClient:
    def __init__(self, token: str, *, api_root: str = ...) -> None: ...
    async def call(
        self, method: str, payload: dict | None = ..., *, request_timeout: float = ...
    ) -> Any: ...
    async def upload(
        self, *, method: str, fields: dict, file_field: str, filename: str, content: bytes
    ) -> Any: ...
    async def download_file(self, file_id: str, *, max_bytes: int) -> TelegramFile: ...
    @property
    def is_closed(self) -> bool: ...
    async def close(self) -> None: ...
