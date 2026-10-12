from typing import TYPE_CHECKING

from agentchat.channels.base import Channel, ThreadHistoryChannel

if TYPE_CHECKING:
    from agentchat.channels.slack import Slack, SlackUser

__all__ = ["Channel", "Slack", "SlackUser", "ThreadHistoryChannel"]


def __getattr__(name: str) -> object:
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
