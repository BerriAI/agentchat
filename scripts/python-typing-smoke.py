"""Check public native signatures with mypy; this file does not execute I/O.

Run with MYPYPATH=python mypy --ignore-missing-imports --follow-imports silent
scripts/python-typing-smoke.py, using the project environment's Python executable.
"""

from typing import assert_type

from agentchat import AgentChat, Conversation, Message, MessageContext, Sender
from agentchat.activity import WorkingStatus
from agentchat.channels.base import Channel
from agentchat.channels.discord import Discord
from agentchat.channels.imessage import IMessage
from agentchat.channels.slack import Slack, SlackUser
from agentchat.channels.teams import Teams
from agentchat.channels.telegram import Telegram
from agentchat.channels.telegram.rendering import RenderedText, render
from agentchat.channels.telegram.transport import TelegramBotClient
from agentchat.channels.whatsapp import WhatsApp
from agentchat.state import MemoryState


async def check_api(
    slack: Slack, discord: Discord, teams: Teams, imessage: IMessage,
    telegram: Telegram, whatsapp: WhatsApp, context: MessageContext, message: Message,
) -> None:
    channels: list[Channel] = [slack, discord, teams, imessage, telegram, whatsapp]
    app = AgentChat(channels=channels, state=MemoryState())
    assert_type(app.channels, tuple[Channel, ...])
    assert_type(context.sender, Sender)
    assert_type(context.conversation, Conversation)
    assert_type(await context.reply("hello"), Message)
    assert_type(await context.history(), tuple[Message, ...])
    assert_type(context.working(), WorkingStatus)
    async with context.working() as status:
        assert_type(await status.update("still working"), bool)
    async with app.state.lock(context.conversation.id):
        assert_type(await app.state.history(context.conversation.id), tuple[Message, ...])
    assert_type(await slack.get_user("U123"), SlackUser)
    assert_type(await telegram.reply(message, "hello"), Message)
    assert_type(await whatsapp.reply(message, "hello"), Message)
    assert_type(render("**hello**"), RenderedText)
    assert_type(TelegramBotClient("99:token"), TelegramBotClient)
