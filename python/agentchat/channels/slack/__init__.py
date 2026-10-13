"""Slack adapter, replaceable event sources and reusable provider helpers."""

from typing import TYPE_CHECKING

from agentchat.channels.slack.transport import SlackEventReceiver, SlackEventSource

if TYPE_CHECKING:
    from agentchat.channels.slack.adapter import (
        MemoryThreadSubscriptions,
        Slack,
        SlackUser,
        SlackWebClient,
        ThreadSubscriptions,
    )
    from agentchat.channels.slack.socket_mode import SocketMode

__all__ = [
    "MemoryThreadSubscriptions",
    "Slack",
    "SlackEventReceiver",
    "SlackEventSource",
    "SlackUser",
    "SlackWebClient",
    "SocketMode",
    "ThreadSubscriptions",
]


def __getattr__(name: str) -> object:
    if name == "SocketMode":
        from agentchat.channels.slack.socket_mode import SocketMode

        return SocketMode
    if name in {"MemoryThreadSubscriptions", "Slack", "SlackUser", "SlackWebClient",
                "ThreadSubscriptions"}:
        from agentchat.channels.slack import adapter

        return getattr(adapter, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
