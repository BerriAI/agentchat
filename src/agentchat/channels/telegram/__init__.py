from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchat.channels.telegram.adapter import Telegram

__all__ = ["Telegram"]


def __getattr__(name: str) -> object:
    if name != "Telegram":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from agentchat.channels.telegram.adapter import Telegram
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "aiohttp":
            raise ModuleNotFoundError(
                "Install Telegram support with: pip install 'agentchat[telegram]'",
                name=exc.name,
            ) from exc
        raise
    return Telegram


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
