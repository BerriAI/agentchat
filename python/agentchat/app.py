"""Python types for the native application runtime."""
from collections.abc import Awaitable, Callable

from agentchat._native import AgentChat
from agentchat.context import MessageContext

MessageHandler = Callable[[MessageContext], Awaitable[str | None]]
__all__ = ["AgentChat", "MessageHandler"]
