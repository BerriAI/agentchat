# agentchat

Connect chat platforms to any Python agent

AgentChat handles messages, conversations, acknowledgements, deduplication, and replies. Your application owns the agent

## Install

```bash
pip install -e ".[openai]"
```

## Slack and OpenAI Agents SDK

```python
import asyncio

from agents import Agent, Runner
from agentchat import AgentChat
from agentchat.channels import Slack
from agentchat.integrations import to_openai_input

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant",
)

app = AgentChat(channels=[Slack.from_env()])


@app.on_message
async def respond(context):
    history = await context.conversation.history(limit=30)
    result = await Runner.run(agent, input=to_openai_input(history))
    return str(result.final_output)


asyncio.run(app.run())
```

AgentChat does not depend on an agent framework. The handler can call OpenAI Agents SDK, Pydantic AI, LangGraph, or your own runtime

## Slack setup

Create a Slack app with Socket Mode enabled, then add these bot scopes:

- `app_mentions:read`
- `chat:write`
- `im:history`

Subscribe to the `app_mention` and `message.im` bot events. Install the app, then export its tokens:

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export OPENAI_API_KEY="sk-..."
```

Run the example:

```bash
python examples/slack_openai_agent.py
```

Send the bot a direct message or mention it in a channel. Follow-up direct messages share one conversation. Channel mentions continue inside their Slack thread

## Core interface

```python
@app.on_message
async def respond(context):
    return await my_agent(context)
```

The context exposes the normalized message, sender, and conversation:

```python
context.message
context.sender
context.conversation

history = await context.conversation.history(limit=30)
await context.reply("Hello")
```

Incoming messages are acknowledged, deduplicated, and serialized by conversation before the handler runs. Returning a string posts it to the originating Slack DM or thread

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```
