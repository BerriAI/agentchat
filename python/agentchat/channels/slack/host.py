"""Slack protocol helpers for applications that own authentication and durable ingress."""
from typing import Literal, NotRequired, TypedDict

from agentchat._native import SlackHost as SlackHost


class SlackRouting(TypedDict):
    text: str
    mentioned: bool
    excluded: bool

class ThreadOrigin(TypedDict):
    team_id: str
    channel_id: str
    thread_ts: str
    message_ts: str

class CurrentReference(ThreadOrigin):
    user_id: str
    data: dict[str, object]

class SlackHostEvent(ThreadOrigin, SlackRouting):
    user_id: str
    direct: bool
    files: list[dict[str, object]]
    reference: CurrentReference

class ContextMessage(TypedDict):
    ts: str
    author: str
    content: str
    truncated: bool

class ThreadContext(ThreadOrigin):
    status: Literal["complete", "partial", "unavailable"]
    messages: list[ContextMessage]
    error: NotRequired[str]
    warning: NotRequired[str]
