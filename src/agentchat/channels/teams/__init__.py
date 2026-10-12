"""Teams adapter and provider-local helpers, loaded on demand."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchat.channels.teams.adapter import Teams

__all__ = ["Teams"]


def __getattr__(name: str) -> object:
    if name != "Teams":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from agentchat.channels.teams.adapter import Teams
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "microsoft_agents":
            raise ModuleNotFoundError(
                "Install Teams support with: pip install 'agentchat[teams]'",
                name=exc.name,
            ) from exc
        raise
    return Teams


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
