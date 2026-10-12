"""Discord adapter and provider-local helpers, loaded on demand."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchat.channels.discord.adapter import Discord

__all__ = ["Discord"]


def __getattr__(name: str) -> object:
    if name != "Discord":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from agentchat.channels.discord.adapter import Discord
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "discord":
            raise ModuleNotFoundError(
                "Install Discord support with: pip install 'agentchat[discord]'",
                name=exc.name,
            ) from exc
        raise
    return Discord


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
