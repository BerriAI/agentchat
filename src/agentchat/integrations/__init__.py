from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchat.integrations.openai_agents import should_reply, to_openai_input

__all__ = ["should_reply", "to_openai_input"]


def __getattr__(name: str) -> object:
    if name in __all__:
        try:
            from agentchat.integrations import openai_agents
        except ModuleNotFoundError as exc:
            if exc.name != "agents":
                raise
            raise ModuleNotFoundError(
                "The OpenAI Agents integration requires openai-agents. "
                "Install it with: pip install 'agentchat[openai]'",
                name=exc.name,
            ) from exc
        return getattr(openai_agents, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
