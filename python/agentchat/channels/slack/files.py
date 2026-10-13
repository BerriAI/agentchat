"""Slack file interfaces; Rust owns transfer policy and sequencing."""
from collections.abc import Awaitable, Callable, Mapping

import aiohttp as aiohttp

from agentchat._native import (
    slack_download_file as download_slack_file,
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

__all__ = [
    "DeliveryGuard", "SlackRequest", "download_slack_file", "upload_bytes",
    "upload_slack_files", "validate_destination",
]
