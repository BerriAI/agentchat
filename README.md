# agentchat

Connect chat platforms to any Python agent. AgentChat handles message delivery,
conversation history, deduplication, and replies; your application owns the agent.

Slack, Discord, Microsoft Teams, iMessage through BlueBubbles, and WhatsApp Business
Cloud API have channel adapters. Each provider lives in its own package and uses
the same message loop.

| Connect | Setup and echo example |
| --- | --- |
| Slack | [Slack guide](docs/slack.md) · [echo](examples/slack_echo.py) |
| Discord | [Discord guide](docs/discord.md) · [echo](examples/discord_echo.py) |
| Microsoft Teams | [Teams guide](docs/teams.md) · [echo](examples/teams_echo.py) |
| iMessage | [BlueBubbles guide](docs/imessage.md) · [echo](examples/imessage_echo.py) |
| WhatsApp | [Cloud API guide](docs/whatsapp.md) · [echo](examples/whatsapp_echo.py) |

The four new adapters support receiving messages and confirmed text replies.
See [shared usage, capabilities, and delivery limits](docs/providers.md) for
multiple providers or an existing agent runner.

## Quickstart

Install from this checkout, then follow the [Slack setup guide](docs/slack.md#slack-setup)
to configure Socket Mode and export `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`:

```bash
pip install -e .
python examples/slack_echo.py
```

The echo example needs no agent framework or model API key:

```python
import asyncio

from agentchat import AgentChat, MessageContext
from agentchat.channels.slack import Slack


async def main():
    app = AgentChat(channels=[Slack.from_env(ack_emoji=None)])

    @app.on_message
    async def respond(context: MessageContext):
        return context.message.text or "Received your attachment."

    try:
        await app.run()
    finally:
        await app.close()


asyncio.run(main())
```

Replace the handler with your agent runner. Use `context.history()` for shared
conversation context, `context.reply()` for replies, and `context.working()` for
an activity indicator. Applications authorize the current sender before fetching
history, subscribing to a conversation, or invoking agent tools.

## Connect an agent

The [OpenAI Agents example](examples/slack_openai_agent.py) includes shared thread
history, subscriptions, an optional reply filter, and working indicators:

```bash
pip install -e ".[openai]"
export OPENAI_API_KEY="..."
python examples/slack_openai_agent.py
```

Handlers can also call Pydantic AI, LangGraph, or your own runtime. AgentChat does
not select or run an agent framework for you. An application that already owns
history and ordering can [bind directly to the channel](docs/slack.md#using-an-existing-agent-runner).

## Find your way around

| Task | Start here |
| --- | --- |
| Add a chat platform | [Channel adapter guide](docs/adding-a-channel.md) and `src/agentchat/channels/base.py` |
| Change Slack behavior | `src/agentchat/channels/slack/` and `tests/channels/slack/` |
| Change message processing | `src/agentchat/app.py` and `src/agentchat/context.py` |
| Add agent-framework helpers | `src/agentchat/integrations/` |
| Implement conversation storage | `src/agentchat/state/base.py` |
| Understand ownership and compatibility | [Architecture](docs/architecture.md) |

The [Slack guide](docs/slack.md) covers setup, working indicators, user profiles,
thread history, incoming attachments, rich replies, uploads, and mirrored inputs.
Existing `from agentchat.channels import Slack` imports remain supported.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
python -m pytest
```

Provider behavior tests live under `tests/channels/`, framework tests under
`tests/integrations/`, and shared adapter contracts under `tests/contracts/`.
Shared fakes and event builders live in `tests/support/`.
