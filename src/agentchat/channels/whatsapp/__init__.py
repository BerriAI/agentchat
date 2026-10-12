"""WhatsApp adapter and provider-local helpers, loaded on demand."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchat.channels.whatsapp.adapter import WhatsApp

__all__ = ["WhatsApp"]


def __getattr__(name: str) -> object:
    if name != "WhatsApp":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from agentchat.channels.whatsapp.adapter import WhatsApp
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "aiohttp":
            raise ModuleNotFoundError(
                "Install WhatsApp support with: pip install 'agentchat[whatsapp]'",
                name=exc.name,
            ) from exc
        raise
    return WhatsApp


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
