from __future__ import annotations

from agents.items import TResponseInputItem

from agentchat.models import Message


def to_openai_input(messages: tuple[Message, ...]) -> list[TResponseInputItem]:
    return [{"role": message.role, "content": message.text} for message in messages]
