from importlib import import_module
from typing import TYPE_CHECKING

from agentchat.channels.base import Channel, ThreadHistoryChannel

if TYPE_CHECKING:
    from agentchat.channels.discord import Discord
    from agentchat.channels.imessage import IMessage
    from agentchat.channels.slack import Slack, SlackUser
    from agentchat.channels.teams import Teams
    from agentchat.channels.whatsapp import WhatsApp

__all__ = [
    "Channel",
    "Discord",
    "IMessage",
    "Slack",
    "SlackUser",
    "Teams",
    "ThreadHistoryChannel",
    "WhatsApp",
]

_PROVIDERS = {
    "Discord": "discord",
    "Teams": "teams",
    "IMessage": "imessage",
    "WhatsApp": "whatsapp",
}


def __getattr__(name: str) -> object:
    if name in _PROVIDERS:
        return getattr(import_module(f"agentchat.channels.{_PROVIDERS[name]}"), name)
    if name in {"Slack", "SlackUser"}:
        try:
            from agentchat.channels import slack
        except ModuleNotFoundError as exc:
            if exc.name not in {"slack_sdk", "aiohttp"}:
                raise
            raise ModuleNotFoundError(
                "Slack requires slack-sdk and aiohttp. "
                "Install AgentChat with its dependencies: pip install agentchat",
                name=exc.name,
            ) from exc
        return getattr(slack, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
