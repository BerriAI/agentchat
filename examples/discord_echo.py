"""Echo messages after completing docs/discord.md; no model API key required."""

import asyncio
import logging

from agentchat import AgentChat, MessageContext
from agentchat.channels.discord import Discord


async def main() -> None:
    app = AgentChat(channels=[Discord.from_env()])

    @app.on_message
    async def respond(context: MessageContext) -> str:
        # Authorize the sender here before replacing this echo with an agent.
        return context.message.text or "Received your attachment."

    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
