# Architecture

AgentChat connects messaging platforms to application-owned agent runners. Each
platform implements the `Channel` protocol and emits the same `Message` values.
The application passes channel instances directly to `AgentChat`; there is no
provider registry or framework superclass.

```text
provider event
    │
    ▼
channel adapter       decode payload, apply provider routing/subscription rules
    │ Message
    ▼
AgentChat             claim ID → lock conversation → append → dispatch
    │ MessageContext
    ▼
application handler   authorize sender → fetch context → invoke chosen agent
    │ reply
    ▼
channel adapter       deliver to the source destination → return Message
    │
    ▼
AgentChat             append the returned reply to local state
```

## Ownership

| Module | Owns |
| --- | --- |
| `app.py` | Channel lifecycle, deduplication, ordering, dispatch and local-history writes |
| `context.py` | Handler-facing `MessageContext` and `Conversation` operations |
| `models.py` | Shared message, sender, attachment and reply value objects |
| `activity.py` | Refreshing and clearing in-process working indicators |
| `channels/base.py` | Required channel operations and optional capability protocols |
| `channels/<provider>/` | Provider SDKs, credentials, payloads, connection resources and delivery |
| `integrations/` | Optional helpers for agent frameworks |
| `state/` | Claiming message IDs, conversation locks, local history and storage |

Core modules depend on shared models and channel contracts, never a concrete
provider or agent framework. Adapters do not import each other. Framework helpers
consume normalized messages and do not own the channel lifecycle.

The core records an adapter's returned reply and does not independently validate
provider receipts. Adapters own delivery confirmation and error propagation.

`context.history()` uses native thread history when supported, otherwise local
state. Native history failures propagate rather than silently returning partial
context. `context.conversation.history()` always reads only local state. File
bytes and working indicators do not enter conversation history.

Applications own sender authorization, agent execution, durable work and delivery
recovery. Direct channel binding is also supported when an application already
owns deduplication, ordering and history; see the [Slack guide](slack.md#using-an-existing-agent-runner).

## Provider containment

The shared channel namespace contains only adapter contracts. A provider owns
its implementations, exports and optional dependency loading:

```text
channels/
├── __init__.py        shared contract exports only
├── base.py            Channel and optional capability protocols
└── slack/
    ├── __init__.py    lazy public Slack exports
    ├── adapter.py     shared Slack message behavior and Web API operations
    ├── transport.py   SlackEventSource and its event receiver contract
    ├── socket_mode.py Socket Mode connection, acknowledgements and shutdown
    ├── events.py      pure event/attachment normalization
    ├── rendering.py   pure text and rich-reply formatting
    ├── files.py       uploads and bounded downloads
    └── mirroring.py   external-input rendering and sender/role recovery
```

`base.py` is shared because it defines what any messaging adapter must implement.
Every Slack-specific module lives below `slack/`. There are no sibling Slack
compatibility modules or provider aliases in the shared namespace.

Discord, Teams, iMessage, WhatsApp, and Telegram use the same package boundary with
`adapter.py`, normalization in `events.py`, and text validation in `rendering.py`.
Discord, BlueBubbles, and Telegram have small REST transport modules too. The provider-neutral
private helper `agentchat/_delivery.py` shares bounded background dispatch and
cleanup. Provider authentication, acknowledgement, and overflow policy stay in
each adapter. See the [provider guides](providers.md) for supported operations.

## Composition

The generic adapter boundary remains `Channel`. Within Slack, replace event
delivery independently of message behavior:

```text
SocketMode ────────────┐
                       ├── SlackEventSource ── Slack.handle_event ── AgentChat
application event feed ┘                           │
                                                  ├── subscriptions/history
                                                  └── replies/files/status
```

`Slack.run()` authenticates the bot and checks the configured workspace before
starting its source. The source calls the same `handle_event` method for every
payload, so normalization, subscription filtering, sender identity and replies
remain shared. `Slack.close()` and `is_connected()` delegate source lifecycle.
The default Socket Mode implementation owns socket SDK imports, acknowledgement,
bounded event tasks and graceful shutdown.

Supply exactly one of `app_token` (the Socket Mode convenience) or `event_source`
(an application-owned source). A custom source requires no app token and does
not import or instantiate Socket Mode. See the [composition guide](slack.md#composing-event-sources).

| Replaceable concern | Injection point | Responsibility |
| --- | --- | --- |
| Incoming events | `Slack(event_source=...)` | Deliver authenticated Slack events; own input lifecycle |
| Slack Web API calls | `Slack(web_client=...)` | Provide the existing `SlackWebClient` operations |
| Followed threads | `Slack(thread_subscriptions=...)` | Persist or expire Slack thread subscriptions |
| Conversation state | `AgentChat(state=...)` | Claim events, serialize conversations, retain history |
| Agent execution | `@app.on_message` | Authorize sender and call the application's runner |

Event decoding never downloads files. Standalone event and rendering helpers can
be imported without provider SDKs. File transport helpers accept explicit request
callbacks and authorization guards. Applications retain HTTP signature checking,
durable queues, authorization and ambiguous-delivery recovery.

Raw event envelopes, acknowledgement and connection behavior are provider-specific;
they do not belong in the generic `Channel` contract. A new provider can use a
similar composition seam when it has multiple input mechanisms, without inheriting
Slack code or changing `AgentChat`.

## Import migration

This cleanup deliberately removes old Slack shortcut/helper imports. Update them
as follows; no hidden aliases are retained:

| Previous import | Current import |
| --- | --- |
| `from agentchat.channels import Slack, SlackUser` | `from agentchat.channels.slack import Slack, SlackUser` |
| `agentchat.channels.slack_files.slack_attachments` | `agentchat.channels.slack.events.slack_attachments` |
| `agentchat.channels.slack_files.download_slack_file` | `agentchat.channels.slack.files.download_slack_file` |
| `agentchat.channels.slack_media.upload_slack_files` | `agentchat.channels.slack.files.upload_slack_files` |
| `agentchat.channels.slack_media.rich_payload` | `agentchat.channels.slack.rendering.rich_payload` |
| `agentchat.channels.slack_mirror.mirror_payload` | `agentchat.channels.slack.mirroring.mirror_payload` |

Other rendering helpers (`text_blocks`), file helpers (`upload_bytes`,
`validate_destination`, `SlackRequest`, `DeliveryGuard`) and `read_mirror` are
available from the corresponding canonical modules above.

Root-level message and context imports are unchanged. Importing the core does
not import Slack, aiohttp or OpenAI Agents. Installation defaults are unchanged:
Slack dependencies remain installed with the base package, while OpenAI Agents
still uses the `openai` extra.

Discord, Teams, and iMessage use separate optional extras. WhatsApp and Telegram use aiohttp
from the base install. Requesting one provider does not import the others.
