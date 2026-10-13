import discord

from agentchat.models import Message

def normalize(event: discord.Message, *, bot_id: int) -> Message | None: ...
