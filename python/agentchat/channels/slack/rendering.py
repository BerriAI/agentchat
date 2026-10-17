"""Slack rendering implemented in Rust."""
from typing import Literal, TypedDict

from agentchat._native import slack_message_request as message_request
from agentchat._native import slack_rich_payload as rich_payload
from agentchat._native import slack_text_blocks as text_blocks


class SlackMessageRequest(TypedDict):
    method: Literal["chat.postMessage", "chat.update"]
    payload: dict[str, object]


__all__ = ['SlackMessageRequest', 'message_request', 'rich_payload', 'text_blocks']
