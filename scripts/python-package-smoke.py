"""Run outside python/ to verify an installed wheel/sdist and public Python API."""
import asyncio
from dataclasses import replace
from importlib.resources import files

from agentchat import AgentChat, Message, Sender
from agentchat._native import core_version
from agentchat.channels.slack.events import to_message


class EchoChannel:
    name = "smoke"

    def bind(self, receiver):
        self.receiver = receiver

    async def run(self):
        message = Message("smoke:1", "smoke:thread", self.name, Sender("user"), "hello", "user")
        await self.receiver(self, message)
        await self.receiver(self, message)

    async def reply(self, source, content):
        return replace(source, id="smoke:reply", text=content, role="assistant")

    async def close(self):
        pass


async def main():
    assert files("agentchat").joinpath("py.typed").is_file()
    assert files("agentchat").joinpath("app.pyi").is_file()
    event = {"type": "message", "channel_type": "im", "channel": "D123",
             "user": "U123", "text": "hello", "ts": "1.000001"}
    normalized = to_message({"team_id": "T123", "event": event}, event,
                            bot_user_id="U_BOT", workspace_id="T123")
    assert normalized.text == "hello" and normalized.channel == "slack"
    app = AgentChat(channels=[EchoChannel()])

    @app.on_message
    async def respond(context):
        assert context.message.text == "hello"
        assert len(await context.history()) == 1
        return "world"

    try:
        await app.run()
        history = await app.state.history("smoke:thread")
        assert [message.text for message in history] == ["hello", "world"]
    finally:
        await app.close()
    print(f"Python package smoke passed (Rust core {core_version})")


asyncio.run(main())
