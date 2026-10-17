# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, BinaryIO, TypeVar

from agentchat.models import Attachment, DownloadedFile, UploadedFile, UploadFile

SlackRequest = Callable[[str, dict[str, object]], Awaitable[Mapping[str, object]]]
DeliveryGuard = Callable[[], Awaitable[None]]

def validate_destination(channel_id: str, thread_ts: str | None) -> None: ...
async def upload_bytes(url: str, raw: bytes) -> None: ...
async def upload_slack_files(
    files: Sequence[UploadFile],
    *,
    request: SlackRequest,
    channel_id: str,
    thread_ts: str | None = ...,
    before_send: DeliveryGuard | None = ...,
) -> tuple[UploadedFile, ...]: ...
async def download_slack_file(
    attachment: Attachment,
    *,
    bot_token: str,
    request: SlackRequest,
    max_bytes: int = ...,
    before_download: DeliveryGuard | None = ...,
) -> DownloadedFile: ...

CANVAS_MIME: str

class CanvasFormatError(ValueError): ...

class SlackFileError(ValueError):
    code: str
    retryable: bool
    retry_after: float | None
    history: list[dict[str, Any]]
    retry_blocker: str
    def __init__(
        self, code: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None: ...
    def diagnostic(self) -> dict[str, Any]: ...

class SlackTransportError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        dispatched: bool | None = None,
        rejected: bool = False,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None: ...

class SlackDownloadedFile:
    name: str
    raw: BinaryIO
    media_type: str
    representation: str
    history: list[dict[str, Any]]
    state: dict[str, Any]

class SlackFileReader:
    def __init__(
        self,
        *,
        max_bytes: int,
        read_attempts: int = 2,
        read_budget: float = 90.0,
        retry_delay: float = 1.0,
    ) -> None: ...
    async def read(
        self,
        file_id: str,
        *,
        token: Callable[[], Awaitable[str]],
        request: Callable[[str, dict[str, object], str], Awaitable[Mapping[str, object]]],
        authorize: Callable[[], Awaitable[None]],
    ) -> SlackDownloadedFile: ...

_T = TypeVar("_T")

def file_operation(operation: Callable[..., _T], *args: Any) -> Awaitable[_T]: ...
def is_canvas(file: Mapping[str, object]) -> bool: ...
def bounded_preview(text: str, *, truncated: bool = False) -> str: ...
def inspect_canvas(raw: BinaryIO) -> tuple[str, bytes, str]: ...
