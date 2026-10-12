"""Compatibility exports; use :mod:`agentchat.channels.slack.files` and `.rendering`."""

from agentchat.channels.slack.files import (
    DeliveryGuard,
    SlackRequest,
    upload_bytes,
    upload_slack_files,
    validate_destination,
)
from agentchat.channels.slack.rendering import rich_payload, text_blocks

__all__ = [
    "DeliveryGuard",
    "SlackRequest",
    "rich_payload",
    "text_blocks",
    "upload_bytes",
    "upload_slack_files",
    "validate_destination",
]
