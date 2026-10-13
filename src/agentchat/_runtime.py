"""Execute Rust turn effects in the caller's Python task.

The native engine owns ordering. This adapter only invokes the existing Python
protocols: it neither serializes user objects nor reschedules host callbacks.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from agentchat._native import Effect, NativeTurn
from agentchat.context import MessageContext

if TYPE_CHECKING:
    from agentchat.app import AgentChat, MessageHandler
    from agentchat.channels.base import Channel
    from agentchat.models import Message


async def receive(
    app: AgentChat, channel: Channel, message: Message, handler: MessageHandler,
) -> None:
    turn = NativeTurn()
    async with AsyncExitStack() as scope:
        while turn.effect != Effect.Complete:
            match turn.effect:
                case Effect.Claim:
                    turn.claimed(await app.state.claim(message.id))
                case Effect.AcquireConversation:
                    await scope.enter_async_context(app.state.lock(message.conversation_id))
                    turn.acquired()
                case Effect.AppendIncoming:
                    await app.state.append(message)
                    turn.incoming_appended()
                case Effect.InvokeHandler:
                    turn.handler_completed(await handler(MessageContext(app, channel, message)))
                case Effect.SendReply:
                    # The public Python operation already sends and appends.
                    # Call it intact so application overrides keep working.
                    await app.reply(channel, message, turn.reply_text)
                    turn.reply_sent()
                case Effect.AppendReply:
                    # Acknowledge the append performed by the host operation;
                    # never write the confirmed reply to history a second time.
                    turn.reply_appended()
