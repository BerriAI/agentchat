# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Awaitable, Callable, Mapping, Sequence

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
