"""Run outside src/ to verify an installed wheel/sdist and public Python API."""
import asyncio
from dataclasses import replace

from agentchat._native import core_version

from agentchat import AgentChat, Message, Sender


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
    for backend in ("rust", "python"):
        app = AgentChat(channels=[EchoChannel()], backend=backend)

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
