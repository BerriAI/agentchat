import asyncio

from agents import Agent, Runner

from agentchat import AgentChat, MessageContext
from agentchat.channels import Slack
from agentchat.integrations import to_openai_input
from agentchat.state import MemoryState

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant. Keep responses concise and useful.",
)

slack = Slack.from_env()
app = AgentChat(channels=[slack], state=MemoryState())


@app.on_message
async def respond(context: MessageContext) -> str:
    await slack.subscribe(context.message)
    history = await context.conversation.history(limit=30)
    result = await Runner.run(agent, input=to_openai_input(history))
    return str(result.final_output)


if __name__ == "__main__":
    asyncio.run(app.run())
