# agentchat

a minimal sdk to connect your agents to slack, teams, imessage, whatsapp.

use any SDK (Claude Agents, OpenAI Agents, Langchain DeepAgents, Pydantic Agents) and instantly connect it to all supported channels.

## Quickstart

Use the OpenAI Agents SDK with Slack.

Clone this repository, use Python 3.11+, and install AgentChat in your virtual
environment:

```sh
git clone https://github.com/BerriAI/agentchat.git
cd agentchat
python -m pip install -e ".[openai]"
```

[Configure your Slack app](docs/slack.md#slack-setup), then
set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `OPENAI_API_KEY`. Set
`OPENAI_MODEL` to a model available to your account.

Save this as `bot.py`:

```python
import asyncio
import os

from agents import Agent, Runner, SQLiteSession

from agentchat import AgentChat, MessageContext
from agentchat.channels.slack import Slack
# from agentchat.channels.teams import Teams
# from agentchat.channels.imessage import IMessage
# from agentchat.channels.whatsapp import WhatsApp
# from agentchat.channels.discord import Discord

slack = Slack.from_env(ack_emoji=None)
app = AgentChat(channels=[
    slack,
    # Teams.from_env(),
    # IMessage.from_env(),
    # WhatsApp.from_env(),
    # Discord.from_env(),
])

agent = Agent(name="Assistant", model=os.environ["OPENAI_MODEL"])
sessions = {}


@app.on_message
async def respond(context: MessageContext) -> str:
    key = context.conversation.id
    if key not in sessions:
        sessions[key] = SQLiteSession(key)

    result = await Runner.run(
        agent,
        input=context.message.text,
        session=sessions[key],
    )
    return str(result.final_output)


async def main():
    try:
        await app.run()
    finally:
        await app.close()


asyncio.run(main())
```

Run `python bot.py`, then DM the Slack bot or mention it in a channel thread.
AgentChat routes the returned answer to the same conversation. The SDK's session
retains the conversation between messages.
[OpenAI sessions documentation](https://developers.openai.com/api/docs/guides/agents/running-agents)

To enable another channel, install its extra and configure its credentials
before uncommenting its import and channel entry. See
[supported channels](#supported-channels) for setup guides.

To receive subsequent channel-thread replies without another mention, add
`await slack.subscribe(context.message)` inside your handler after checking the
sender's access. For shared threads, retain speaker identity in your agent's
input formatting. Conversation membership does not grant tool permissions.

## Other agent SDKs

Call your SDK from the handler. Use `context.message.text` for input, return a
string for output, and map `context.conversation.id` to your SDK's state.
You do not need an AgentChat adapter for each SDK.

Keep the quickstart's `asyncio`/`os` and AgentChat imports, Slack setup, and
`main()` function. Replace the OpenAI import, agent/session setup, and handler
with one example below, before the startup call.

Use `context.conversation.id` to keep SDK state separate for each chat. These
examples handle text replies; add a tool approval flow if your agent needs one.

### Claude Agent SDK

Install `claude-agent-sdk` and configure `ANTHROPIC_API_KEY` or another supported
SDK authentication method. This example disables built-in tools for text chat.

```python
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

sessions = {}


@app.on_message
async def respond(context: MessageContext) -> str:
    key = context.conversation.id
    options = ClaudeAgentOptions(resume=sessions.get(key), tools=[])

    result = None
    async for event in query(prompt=context.message.text, options=options):
        if isinstance(event, ResultMessage):
            sessions[key] = event.session_id
            result = event

    if result is None or result.is_error or not result.result:
        raise RuntimeError("Claude did not return a successful answer")
    return result.result
```

The mapping connects each chat conversation to its Claude session.
[Claude session documentation](https://code.claude.com/docs/en/agent-sdk/sessions)

### Deep Agents

Install `deepagents` and your model integration. For example, install
`langchain-anthropic`, configure `ANTHROPIC_API_KEY`, and set `DEEP_AGENT_MODEL`
to an accessible `anthropic:<model-name>`.

```python
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

agent = create_deep_agent(
    model=os.environ["DEEP_AGENT_MODEL"],
    checkpointer=InMemorySaver(),
)


@app.on_message
async def respond(context: MessageContext) -> str:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": context.message.text}]},
        config={"configurable": {"thread_id": context.conversation.id}},
    )
    if result.get("__interrupt__"):
        raise RuntimeError("This example does not handle approval requests")
    answer = result["messages"][-1]
    if answer.type != "ai" or answer.tool_calls or not answer.text:
        raise RuntimeError("The agent did not return a final text answer")
    return answer.text
```

The checkpointer keeps a separate conversation for each thread ID. Deep Agents
retains its standard planning, virtual filesystem, and subagent tools.
[Deep Agents quickstart](https://docs.langchain.com/oss/python/deepagents/quickstart)

### Pydantic AI

Install `pydantic-ai-slim[openai]`. Reuse `OPENAI_API_KEY` and `OPENAI_MODEL`
from the quickstart.

```python
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

agent = Agent(f"openai:{os.environ['OPENAI_MODEL']}", output_type=str)
histories: dict[str, list[ModelMessage]] = {}


@app.on_message
async def respond(context: MessageContext) -> str:
    key = context.conversation.id
    result = await agent.run(
        context.message.text,
        message_history=histories.get(key, []),
    )
    histories[key] = result.all_messages()
    return result.output
```

`all_messages()` includes the previous history and the current run. Replace the
stored history with this list after each turn.

### LangGraph SDK

Install `langgraph-sdk`. Run a LangGraph Agent Server with a graph that accepts
and returns a `messages` state. Set `LANGGRAPH_URL` and `LANGGRAPH_ASSISTANT_ID`
(the graph name or assistant UUID). Set `LANGSMITH_API_KEY` if your server
requires authentication.

```python
from langgraph_sdk import get_client

client = get_client(
    url=os.environ["LANGGRAPH_URL"],
    api_key=os.getenv("LANGSMITH_API_KEY"),
)
threads: dict[str, str] = {}


@app.on_message
async def respond(context: MessageContext) -> str:
    key = context.conversation.id
    if key not in threads:
        thread = await client.threads.create()
        threads[key] = thread["thread_id"]

    result = await client.runs.wait(
        threads[key],
        os.environ["LANGGRAPH_ASSISTANT_ID"],
        input={"messages": [{"role": "user", "content": context.message.text}]},
    )
    if result.get("__interrupt__"):
        raise RuntimeError("The graph is waiting for approval")
    answer = result["messages"][-1]
    if answer["type"] != "ai" or answer.get("tool_calls"):
        raise RuntimeError("The graph did not return a final answer")
    content = answer["content"]
    if not isinstance(content, str) or not content:
        raise RuntimeError("This example expects a text answer")
    return content
```

The server stores the thread history. The dictionary maps each chat conversation
to its server thread. Adjust the response extraction if your graph returns a
different state schema or content blocks.

These examples keep their session mappings, histories, and checkpoints in
memory. To resume after a restart, persist this state using your application's
store and the SDK's persistence support. Claude also needs its underlying session data;
LangGraph SDK needs the mapping to its server threads.

## MessageContext reference

Your `@app.on_message` handler receives a `MessageContext`:

```python
from agentchat import MessageContext


@app.on_message
async def respond(context: MessageContext) -> str | None:
    return f"You said: {context.message.text}"
```

Return a string to send a reply. If you call `await context.reply(...)`, return
`None` to avoid sending the answer twice.

### Context fields

| Field | Type | Description |
| --- | --- | --- |
| `context.message` | `Message` | The normalized incoming message. |
| `context.sender` | `Sender` | The sender; the same object as `context.message.sender`. |
| `context.conversation` | `Conversation` | The conversation identity and access to stored history. |

### Message fields

| Field | Type | Description |
| --- | --- | --- |
| `context.message.id` | `str` | Normalized message identity used for deduplication. |
| `context.message.conversation_id` | `str` | The conversation this message belongs to. |
| `context.message.channel` | `str` | Channel adapter name, such as `"slack"`. |
| `context.message.sender` | `Sender` | Sender identity. |
| `context.message.text` | `str` | Message text; may be empty for file-only input. |
| `context.message.role` | `Literal["user", "assistant"]` | Message role. |
| `context.message.metadata` | `Mapping[str, object]` | Provider-specific metadata; defaults to an empty mapping. |
| `context.message.thread_id` | `str \| None` | Provider thread ID, or `None` for an unthreaded conversation. |
| `context.message.addressed` | `bool` | Whether the message addresses the bot; defaults to `True`. Slack sets this for DMs/mentions; untagged subscribed replies are `False`. |
| `context.message.attachments` | `tuple[Attachment, ...]` | File references; defaults to `()`. File bytes are not downloaded automatically. |

### Sender and conversation fields

| Field | Type | Description |
| --- | --- | --- |
| `context.sender.id` | `str` | Provider sender ID. |
| `context.sender.display_name` | `str \| None` | Display name when supplied by the adapter; defaults to `None`. |
| `context.conversation.id` | `str` | Same identity as `context.message.conversation_id`; suitable for mapping to your SDK's conversation state. |

The conversation ID identifies the chat. SDK session IDs and checkpoints remain
under your application's control; switching SDKs does not transfer their state.

### Context methods

Return types below describe the value after `await`, except `working()`, which
returns an async context manager.

| Method | Returns | Description |
| --- | --- | --- |
| `await context.history(*, limit: int = 30)` | `tuple[Message, ...]` | Shared history including the current message; `limit` must be 2–100. Uses native thread history when available, otherwise the configured AgentChat state. |
| `await context.reply(content: str)` | `Message` | Sends a text reply and returns the confirmed outgoing message. |
| `await context.reply_rich(content: RichReply)` | `Message` | Sends rich content when the channel supports it. Blocks and attachments use the provider's format. |
| `await context.download_attachment(attachment: Attachment, *, max_bytes: int = 10 * 1024 * 1024)` | `DownloadedFile` | Downloads a reference from the current message, up to 10 MiB by default. |
| `await context.upload_files(files: Sequence[UploadFile])` | `tuple[UploadedFile, ...]` | Uploads application-provided bytes and returns provider receipts. |
| `await context.set_status(status: str)` | `bool` | Sets a transient activity status; an empty string clears it. Returns `False` when unsupported. |
| `context.working(status: str = "is working…", *, refresh_interval: float = 60)` | `WorkingStatus` | Maintains a best-effort status inside `async with` and clears it on exit. Refresh interval must be greater than 0 and at most 90 seconds. |

`await context.conversation.history(*, limit: int | None = None)` also returns
`tuple[Message, ...]`, but reads only messages in the configured AgentChat state.
It does not fetch the provider's thread history.

Authorize the sender before reading history or downloading files. Rich replies,
uploads, and downloads raise `TypeError` when the channel lacks that capability.

For example, inside your handler:

```python
async with context.working() as activity:
    await activity.update("is preparing the answer…")
    await context.reply("Here is your answer.")
return None
```

`await activity.update(status: str)` returns `bool`. Working indicators are
presentation state and do not add messages to conversation history.

### File and rich-message types

Import these types from `agentchat`. Optional fields default to `None` or empty tuples.

```python
from agentchat import (
    Attachment,
    DownloadedFile,
    RichReply,
    UploadedFile,
    UploadFile,
)
```

| Type | Fields |
| --- | --- |
| `Attachment` | `id: str`, `name: str \| None = None`, `media_type: str \| None = None`, `size: int \| None = None` (bytes). Metadata is advisory. |
| `DownloadedFile` | `id: str`, `name: str`, `content: bytes`, `media_type: str \| None = None`. |
| `UploadFile` | `filename: str`, `content: bytes`, `title: str \| None = None`. |
| `UploadedFile` | `id: str`, `permalink: str \| None = None`. |
| `RichReply` | `text: str`, `blocks: tuple[Mapping[str, object], ...] = ()`, `attachments: tuple[Mapping[str, object], ...] = ()`. |

`RichReply.attachments` contains provider-specific rich-message objects;
`Message.attachments` contains file references. Upload receipts do not
automatically add a message or binary content to conversation history.

[Context implementation](src/agentchat/context.py) · [Message and file types](src/agentchat/models.py)

## Supported channels

| Channel | Connection | Install extra | Setup |
| --- | --- | --- | --- |
| Slack | Socket Mode | Included | [Slack guide](docs/slack.md) |
| Microsoft Teams | Authenticated HTTPS endpoint | `teams` | [Teams guide](docs/teams.md) |
| iMessage | BlueBubbles on a Mac signed into Messages | `imessage` | [iMessage guide](docs/imessage.md) |
| WhatsApp | Business Cloud API with signed webhooks | `whatsapp` | [WhatsApp guide](docs/whatsapp.md) |
| Discord | Bot gateway | `discord` | [Discord guide](docs/discord.md) |

Combine channel extras with your agent SDK extra, for example:
`python -m pip install -e ".[openai,teams,imessage,whatsapp,discord]"`.

You can use the same agent handler with all five channels. They support incoming
messages and text replies. Slack also supports native thread history, working
indicators, rich replies, and file transfers. See the
[provider guide](docs/providers.md) for capabilities and delivery limits, or the
[channel adapter guide](docs/adding-a-channel.md) to add another channel.
