"""Discord gateway messages to SDK values; no network calls."""

from __future__ import annotations

import re
from types import MappingProxyType

import discord

from agentchat.models import Attachment, Message, Sender


def normalize(event: discord.Message, *, bot_id: int) -> Message | None:
    if event.author.bot or event.webhook_id or event.author.id == bot_id or event.is_system():
        return None
    direct = event.guild is None
    mentioned = any(user.id == bot_id for user in event.mentions)
    reference = event.reference.resolved if event.reference else None
    replies_to_bot = isinstance(reference, discord.Message) and reference.author.id == bot_id
    if not (direct or mentioned or replies_to_bot):
        return None
    text = re.sub(rf"<@!?{bot_id}>", "", event.content).strip()
    attachments = tuple(
        Attachment(
            id=str(item.id), name=item.filename, media_type=item.content_type, size=item.size
        )
        for item in event.attachments
    )
    if not text and not attachments:
        return None
    channel_id = event.channel.id
    guild_id = event.guild.id if event.guild else None
    thread_id = str(channel_id) if isinstance(event.channel, discord.Thread) else None
    return Message(
        id=f"discord:{bot_id}:{channel_id}:{event.id}",
        conversation_id=f"discord:{bot_id}:{guild_id or 'dm'}:{channel_id}",
        channel="discord",
        sender=Sender(id=str(event.author.id), display_name=event.author.display_name),
        text=text,
        role="user",
        thread_id=thread_id,
        attachments=attachments,
        metadata=MappingProxyType(
            {
                "bot_id": bot_id,
                "channel_id": channel_id,
                "guild_id": guild_id,
                "message_id": event.id,
            }
        ),
    )
