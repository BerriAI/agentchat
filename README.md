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

slack = Slack.from_env()
app = AgentChat(channels=[slack])


@app.on_message
async def respond(context):
    await slack.subscribe(context.message)
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
- `channels:history`
- `groups:history` (for private channels)
- `reactions:write` (or set `ack_emoji=None`)

Subscribe to `app_mention`, `message.im`, `message.channels`, and `message.groups`.
Install the app and invite it to the channels where it should respond. Existing
installations need to be reinstalled when adding scopes. Then export its tokens:

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export OPENAI_API_KEY="sk-..."
```

Run the example:

```bash
python examples/slack_openai_agent.py
```

Send the bot a direct message or mention it in a channel. Follow-up direct messages
share one conversation. Plain DMs receive replies in the main DM; explicit DM
thread replies stay in that thread. Channel mentions receive replies under the
original thread root.

Call `await slack.subscribe(context.message)` after your application accepts a
channel conversation to receive its subsequent replies without another mention.
For an authenticated application, check the sender's access **before** subscribing.
Unrelated channel messages and threads are ignored. Other people's mentions remain
in the message text; only the bot's own mention is removed.

Subscriptions default to memory, bounded to the latest 1,000 threads. To retain
them across restarts, pass `thread_subscriptions=` with an object implementing
`async contains(conversation_id) -> bool` and `async add(conversation_id) -> None`.
The application owns that store's expiry policy. Subscribing does not authorize
other participants; check each incoming sender independently.

## Slack user profiles

Slack mentions contain IDs such as `<@U012ABCDEF>`. Resolve one through the native
[`users.info`](https://docs.slack.dev/reference/methods/users.info/) API:

```python
user = await slack.get_user("U012ABCDEF")
user.id
user.team_id
user.display_name
user.email
user.is_bot
user.deleted
```

Profile lookup requires `users:read`; email also requires `users:read.email`.
Reinstall an existing Slack app after adding scopes. Email is optional, and the
SDK never guesses it from a display name. Slack API errors, including missing
scopes and rate limits, propagate to the caller. Malformed profiles raise
`ValueError` without including the profile in the error message.

Lookup is explicit and does not run on every message. Check the requesting user's
authorization first and expose it as a tool in your chosen agent framework when
needed. Applications decide whether a returned user, including Slack Connect
users, bots and deactivated users, is eligible for an operation. Treat profile
fields as data, and match email against the destination system's real records.

## Slack thread context

`await slack.thread_history(message, limit=50)` reads the thread root and recent
replies before the current message from Slack's `conversations.replies` API.
Returned `Message` objects preserve sender IDs; only this bot's messages have the
assistant role. This lets a new process recover a shared conversation without
mixing users' credentials or private agent/tool state.

Authorize the current sender before fetching history. History is context, not
authorization to execute earlier participants' requests. Keep speaker identities
when passing it to an agent. Lookup uses the installed token's history scopes;
Slack API errors propagate. Pagination is bounded to 500 messages and fails on
incomplete results. The returned context retains the root plus the latest replies
within `limit` (2–100); a new root message has no earlier thread history.

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

## Using an existing agent runner

An application that already owns history, authorization and durable deduplication
can bind directly to the public channel API without a second `AgentChat` state store:

```python
slack = Slack.from_env()

async def receive(channel, message):
    # Verify the sender, claim the message ID and run your existing agent here.
    await slack.subscribe(message)
    await channel.reply(message, "Your response")

slack.bind(receive)
asyncio.run(slack.run())
```

With direct binding, the application owns deduplication and request ordering.
`message.id` identifies the Slack message across retries and duplicate
`app_mention`/`message` deliveries. Metadata includes the original `event_id`,
`team_id`, `channel_id`, `channel_type`, `message_timestamp`, and reply timestamp.

`Slack.run()` checks the bot identity. Pass `workspace_id=` to reject a token or
event from another workspace. `await slack.is_connected()` exposes socket readiness.
Socket envelopes are acknowledged before processing in tracked background tasks.
`max_pending_events` defaults to 64; excess envelopes are left unacknowledged for
Slack's retry policy. `await slack.close()` closes the socket, then drains tasks
for up to 20 seconds before cancelling unfinished work. This is bounded in-process
delivery, not a durable queue; applications performing writes need their own journal.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```
