"""Check Slack setup without an agent framework or model API key."""

import asyncio

from agentchat import AgentChat, MessageContext
from agentchat.channels.slack import Slack


async def main() -> None:
    slack = Slack.from_env(ack_emoji=None)
    app = AgentChat(channels=[slack])

    @app.on_message
    async def echo(context: MessageContext) -> str:
        return context.message.text or "Received your attachment."

    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
