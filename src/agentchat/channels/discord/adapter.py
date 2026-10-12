from __future__ import annotations

import asyncio
import logging
import os
from types import MappingProxyType

import discord

from agentchat.channels._delivery import DeliveryTasks
from agentchat.channels.base import MessageReceiver
from agentchat.channels.discord.events import normalize
from agentchat.channels.discord.rendering import text_payload
from agentchat.channels.discord.transport import DiscordRESTClient
from agentchat.models import Message, Sender

logger = logging.getLogger(__name__)


class Discord:
    """Discord gateway transport. Each DM, channel, or native thread is a conversation."""

    name = "discord"

    def __init__(
        self,
        *,
        bot_token: str,
        client: discord.Client | None = None,
        send_client: DiscordRESTClient | None = None,
        max_pending_events: int = 64,
        shutdown_timeout: float = 20,
    ) -> None:
        if not bot_token.strip():
            raise ValueError("Discord bot_token is required")
        self._token = bot_token
        self._delivery = DeliveryTasks(max_pending_events, shutdown_timeout)
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        self._client = client or discord.Client(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # This adapter owns the injected client's message callback and lifecycle.
        self._client.on_message = self._on_message
        self._sender = send_client or DiscordRESTClient(bot_token)
        self._close_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_env(cls) -> Discord:
        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN is required")
        return cls(bot_token=token)

    def bind(self, receiver: MessageReceiver) -> None:
        self._delivery.receiver = receiver

    async def run(self) -> None:
        self._delivery.check_bound()
        try:
            await self._client.start(self._token)
        finally:
            await self.close()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            # Stop admitting events, but retain the HTTP client while replies drain.
            self._delivery.closed = True
            try:
                await self._delivery.close()
            finally:
                try:
                    await self._client.close()
                finally:
                    await self._sender.close()

    async def _on_message(self, event: discord.Message) -> None:
        if self._client.user is None or self._delivery.closed:
            return
        message = normalize(event, bot_id=self._client.user.id)
        if message is not None and not self._delivery.submit(self, message):
            logger.warning("Discord event dropped: max_pending_events reached")

    async def reply(self, source: Message, content: str) -> Message:
        content = text_payload(content)
        bot = self._client.user
        data = source.metadata
        channel_id, message_id = data.get("channel_id"), data.get("message_id")
        if (
            source.channel != self.name
            or bot is None
            or data.get("bot_id") != bot.id
            or not isinstance(channel_id, int)
            or not isinstance(message_id, int)
        ):
            raise ValueError("Source does not belong to this Discord adapter")
        if self._client.is_closed():
            raise RuntimeError("Discord connection is closed")
        self._delivery.check_reply()
        reference = {
            "message_id": str(message_id),
            "channel_id": str(channel_id),
            "fail_if_not_exists": False,
        }
        if data.get("guild_id") is not None:
            reference["guild_id"] = str(data["guild_id"])
        sent_id = await self._sender.send_text(
            channel_id,
            {
                "content": content,
                "message_reference": reference,
                "allowed_mentions": {"parse": [], "replied_user": False},
            },
        )
        if not isinstance(sent_id, int) or sent_id <= 0:
            raise RuntimeError("Discord did not return a message receipt")
        return Message(
            id=f"discord:{bot.id}:{channel_id}:{sent_id}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id=str(bot.id), display_name=bot.display_name),
            text=content,
            role="assistant",
            thread_id=source.thread_id,
            metadata=MappingProxyType({**data, "message_id": sent_id}),
        )
