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

## Slack adapter

```text
channels/slack/
├── __init__.py    public Slack and supporting types
├── adapter.py     lifecycle, subscriptions, history and API coordination
├── events.py      pure event/attachment normalization
├── rendering.py   pure text and rich-reply formatting
├── files.py       uploads and bounded downloads
└── mirroring.py   external-input rendering and sender/role recovery
```

Event decoding does not download files. File transport receives explicit request
callbacks and authorization guards. Slack thread subscriptions stay with Slack;
they are provider routing policy, not the application's conversation store.
The adapter retains socket acknowledgement, bounded background tasks and cleanup.

Keep new provider implementations in their own packages. Add internal files when
their responsibilities justify it; a small provider can start with `adapter.py`.
See [adding a channel](adding-a-channel.md) for the extension contract.

## Public imports and compatibility

Both `from agentchat.channels import Slack` and
`from agentchat.channels.slack import Slack` remain supported. Root-level message
and context imports are unchanged; `agentchat.models.MessageContext` and
`agentchat.models.Conversation` resolve to the same classes as `agentchat.context`.

The `channels` and `integrations` namespaces load concrete implementations only
when requested. Importing the core does not import Slack, aiohttp or OpenAI Agents.
Installation defaults are unchanged: Slack dependencies remain installed with the
base package, while OpenAI Agents still uses the `openai` extra.

The documented legacy helper modules forward to canonical implementations:

| Compatibility module | Canonical location |
| --- | --- |
| `channels.slack_files` | `channels.slack.events` for attachment normalization; `channels.slack.files` for downloads |
| `channels.slack_media` | `channels.slack.rendering` for formatting; `channels.slack.files` for uploads |
| `channels.slack_mirror` | `channels.slack.mirroring` |

New code should import the canonical modules. Tests that patch implementation
details should patch the defining module, not a compatibility re-export.
