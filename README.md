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
user.is_guest
user.is_external
```

Profile lookup requires `users:read`; email also requires `users:read.email`.
Reinstall an existing Slack app after adding scopes. Email is optional, and the
SDK never guesses it from a display name. Slack API errors, including missing
scopes and rate limits, propagate to the caller. Malformed profiles raise
`ValueError` without including the profile in the error message.

Lookup is explicit by default and does not run on every message. Check the requesting user's
authorization first and expose it as a tool in your chosen agent framework when
needed. Applications decide whether a returned user, including Slack Connect
users, bots and deactivated users, is eligible for an operation. Treat profile
fields as data, and match email against the destination system's real records.

## First-use identity provisioning

Use `SlackIdentityProvisioner` to create an application profile on first Slack
use, then link its usage to the same person's verified Google/SSO account through
your account database. This is opt-in and uses the existing organization Slack
installation; individual teammates do not need to authorize Slack again.

```python
from agentchat import AgentChat, SlackIdentity, SlackIdentityProvisioner
from agentchat.channels import Slack

slack = Slack.from_env()

async def upsert_account(identity: SlackIdentity):
    # Implement this atomic, idempotent upsert in your application's account store.
    # identity.key == ("slack", workspace_id, user_id); never key accounts by email.
    return await your_accounts.upsert_external_identity(
        key=identity.key,
        email=identity.email,
        display_name=identity.display_name,
        profile_checked_at=identity.checked_at,
    )

identities = SlackIdentityProvisioner(
    slack,
    workspace_id="T0123456789",
    allowed_email_domains={"berri.ai"},
    on_identity=upsert_account,
)
app = AgentChat(channels=[slack])

@app.on_message
async def respond(context):
    # Apply your application's per-sender authorization before this lookup.
    account = await identities.provision(context.message)
    if account is None:
        return "I could not resolve an eligible company profile."
    await slack.subscribe(context.message)
    return await your_agent(context, account_id=account.id)
```

`your_accounts` and `your_agent` above are application-owned integrations, not SDK
APIs. A runnable memory-only example is provided in
[`examples/slack_identity_provisioning.py`](examples/slack_identity_provisioning.py):

```bash
export SLACK_WORKSPACE_ID="T0123456789"
export SSO_EMAIL_DOMAINS="berri.ai"
# Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN as in Slack setup above.
python examples/slack_identity_provisioning.py
```

Install **`users:read` and `users:read.email`** as bot scopes and reinstall the
Slack app. The helper looks up only a sender whose user message identifies the
configured workspace. Returned IDs and workspace must match; deleted users,
bots, guests, Slack Connect outsiders, missing emails and outside-domain emails
never reach the provisioning callback. Emails are trimmed and lowercased;
dot aliases, plus aliases and subdomains are not merged or inferred.

Successful profiles are cached for one hour (`cache_ttl_seconds`), with at most
1,000 cached entries (`max_cache_entries`). Concurrent lookups of the same sender
share the cache. API lookups time out after five seconds (`lookup_timeout_seconds`);
failures and ineligible results are cached for one minute (`retry_seconds`), then
retried on the next call. There is no background polling or account backfill.
Missing scopes, API errors and expired profiles never fall back to a stale email:
`provision` returns `None` without invoking the callback. Socket Mode envelopes
are acknowledged before this work runs. Callback/database errors propagate to
the application; cancellation also propagates. The helper logs only exception
types, never provider error text or profile contents.
Caches are in-process optimizations, not identity storage or directory synchronization.

The callback runs on each eligible `provision` call, including cache hits, so it
can pick up later SSO links. It returns your own account object or ID unchanged.
Original `Message` and `Sender` values remain immutable. You can also use
`await identities.resolve(message)` to get a `SlackIdentity` without provisioning,
and `await identities.invalidate(user_id)` to force a fresh lookup after a
trusted profile-change event.

Your account store should implement these rules in a transaction:

- Upsert by `identity.key` and enforce its uniqueness, including across workers.
- Create an email-labelled pending profile for Slack-first users. Match an
  existing SSO user only when that exact normalized email uniquely identifies a
  server-verified SSO identity in the same organization.
- After a verified SSO login, reconcile pending Slack profiles for that email,
  including earlier usage. Keep the stable SSO provider and subject, not email,
  as the authenticated identity. Reject duplicate or stale matches for review.
- Preserve explicit administrator links. An email change must not silently
  retarget an existing profile or transfer historical usage to another person.
  Keep original sender IDs in usage records and audit account-link changes.

The SDK supplies Slack profiles and the provisioning hook; **your application
owns durable accounts and verified SSO matching**. A Slack email is not a Google
authentication credential. This helper never creates a web session, grants an
administrator role, or creates a Google Workspace user. SCIM group membership,
deprovisioning and Google token verification remain outside its scope.

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
