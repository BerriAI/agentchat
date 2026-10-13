# Public compatibility signatures; update with the corresponding Rust binding API.
from agents import Agent as Agent
from agents import Model as Model
from agents import RunConfig as RunConfig
from agents import Runner as Runner
from agents.items import TResponseInputItem

from agentchat.models import Message

def to_openai_input(
    messages: tuple[Message, ...], *, include_senders: bool = ...
) -> list[TResponseInputItem]: ...
async def should_reply(
    inputs: list[TResponseInputItem],
    *,
    model: str | Model | None = ...,
    instructions: str = ...,
    run_config: RunConfig | None = ...,
) -> bool: ...
