"""Compatibility exports; use :mod:`agentchat.channels.slack.files` and `.events`."""

from agentchat.channels.slack.events import FILE_ID, slack_attachments
from agentchat.channels.slack.files import DeliveryGuard, SlackRequest, download_slack_file

__all__ = ["FILE_ID", "DeliveryGuard", "SlackRequest", "download_slack_file", "slack_attachments"]
