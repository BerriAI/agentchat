"""Slack file interfaces; Rust owns transfer policy and sequencing."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO

import aiohttp as aiohttp

from agentchat._native import (
    SLACK_CANVAS_MIME as CANVAS_MIME,
)
from agentchat._native import (
    SlackFileReader as SlackFileReader,
)
from agentchat._native import (
    slack_canvas_bounded_preview as bounded_preview,
)
from agentchat._native import (
    slack_download_file as download_slack_file,
)
from agentchat._native import (
    slack_file_diagnostic,
    slack_file_error_init,
    slack_file_error_string,
    slack_transport_error_init,
)
from agentchat._native import (
    slack_file_operation as file_operation,
)
from agentchat._native import (
    slack_inspect_canvas as inspect_canvas,
)
from agentchat._native import (
    slack_is_canvas as is_canvas,
)
from agentchat._native import (
    slack_upload_bytes as upload_bytes,
)
from agentchat._native import (
    slack_upload_files as upload_slack_files,
)
from agentchat._native import (
    slack_validate_destination as validate_destination,
)

SlackRequest = Callable[[str, dict[str, object]], Awaitable[Mapping[str, object]]]
DeliveryGuard = Callable[[], Awaitable[None]]


class CanvasFormatError(ValueError):
    """The bounded export is not readable Canvas HTML; never contains its content."""


class SlackFileError(ValueError):
    """Safe read failure with content-free attempt diagnostics."""

    def __init__(self, code, *, retryable=False, retry_after=None):
        slack_file_error_init(self, code, retryable=retryable, retry_after=retry_after)

    def diagnostic(self):
        return slack_file_diagnostic(self)

    def __str__(self):
        return slack_file_error_string(self)


class SlackTransportError(Exception):
    """Host callback failure metadata. Do not pass response bodies or credentials."""

    def __init__(
        self,
        code,
        *,
        status_code=None,
        dispatched=None,
        rejected=False,
        retryable=False,
        retry_after=None,
    ):
        slack_transport_error_init(
            self,
            code,
            status_code=status_code,
            dispatched=dispatched,
            rejected=rejected,
            retryable=retryable,
            retry_after=retry_after,
        )


@dataclass(frozen=True)
class SlackDownloadedFile:
    """Caller owns and must close raw; original bytes remain available after preview."""

    name: str
    raw: BinaryIO
    media_type: str
    representation: str
    history: list[dict[str, object]]
    state: dict[str, object]


__all__ = [
    "CANVAS_MIME",
    "CanvasFormatError",
    "DeliveryGuard",
    "SlackDownloadedFile",
    "SlackFileError",
    "SlackFileReader",
    "SlackRequest",
    "SlackTransportError",
    "bounded_preview",
    "download_slack_file",
    "file_operation",
    "inspect_canvas",
    "is_canvas",
    "upload_bytes",
    "upload_slack_files",
    "validate_destination",
]
