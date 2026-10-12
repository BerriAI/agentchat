"""IMessage adapter and provider-local helpers, loaded on demand."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchat.channels.imessage.adapter import IMessage

__all__ = ["IMessage"]


def __getattr__(name: str) -> object:
    if name != "IMessage":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from agentchat.channels.imessage.adapter import IMessage
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "socketio":
            raise ModuleNotFoundError(
                "Install IMessage support with: pip install 'agentchat[imessage]'",
                name=exc.name,
            ) from exc
        raise
    return IMessage


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
