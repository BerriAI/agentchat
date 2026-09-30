import asyncio

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from agentchat import AgentChat
from agentchat.channels import Slack
from agentchat.state import MemoryState


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.reactions: list[dict[str, object]] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": str(len(self.posts))}

    async def reactions_add(self, **kwargs):
        self.reactions.append(kwargs)


def test_slack_dm_round_trip_preserves_one_conversation() -> None:
    async def scenario() -> None:
        web_client = FakeSlackClient()
        slack = Slack(bot_token="xoxb-test", app_token="xapp-test", web_client=web_client)
        app = AgentChat(channels=[slack], state=MemoryState())

        @app.on_message
        async def respond(context):
            history = await context.conversation.history()
            return f"I remember {len(history)} messages"

        first_event = {
            "event_id": "event-1",
            "team_id": "team-1",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "dm-1",
                "user": "user-1",
                "ts": "1.0",
                "text": "hello",
            },
        }
        second_event = {
            "event_id": "event-2",
            "team_id": "team-1",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "dm-1",
                "user": "user-1",
                "ts": "2.0",
                "text": "what did I say?",
            },
        }

        await slack.handle_event(first_event)
        await slack.handle_event(second_event)

        assert web_client.posts == [
            {
                "channel": "dm-1",
                "text": "I remember 1 messages",
                "thread_ts": None,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            {
                "channel": "dm-1",
                "text": "I remember 3 messages",
                "thread_ts": None,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        ]

    asyncio.run(scenario())


def test_slack_mention_replies_in_thread_and_deduplicates() -> None:
    async def scenario() -> None:
        web_client = FakeSlackClient()
        slack = Slack(
            bot_token="xoxb-test", app_token="xapp-test", web_client=web_client, bot_user_id="BOT"
        )
        app = AgentChat(channels=[slack], state=MemoryState())

        @app.on_message
        async def respond(context):
            return context.message.text

        event = {
            "event_id": "event-1",
            "team_id": "team-1",
            "event": {
                "type": "app_mention",
                "channel": "channel-1",
                "user": "user-1",
                "ts": "1.0",
                "text": "<@BOT> hello",
            },
        }

        await slack.handle_event(event)
        await slack.handle_event(event)

        assert web_client.posts == [
            {
                "channel": "channel-1",
                "text": "hello",
                "thread_ts": "1.0",
                "unfurl_links": False,
                "unfurl_media": False,
            }
        ]

    asyncio.run(scenario())


def channel_event(ts="1.0", text="<@BOT> inspect <@TARGET>", **extra):
    return {
        "event_id": f"event-{ts}",
        "team_id": "team-1",
        "event": {
            "type": "app_mention",
            "channel": "channel-1",
            "user": "user-1",
            "ts": ts,
            "text": text,
            **extra,
        },
    }


@pytest.mark.parametrize("channel_type", ["channel", "group"])
def test_follows_only_explicitly_subscribed_threads_and_preserves_people(channel_type):
    async def scenario():
        client = FakeSlackClient()
        slack = Slack(
            bot_token="test",
            app_token="test",
            web_client=client,
            workspace_id="team-1",
            bot_user_id="BOT",
        )
        app = AgentChat(channels=[slack])

        @app.on_message
        async def respond(context):
            await slack.subscribe(context.message)
            return context.message.text

        followup = channel_event(
            "2.0", "and their budget?", type="message", channel_type=channel_type, thread_ts="1.0"
        )
        await slack.handle_event(followup)
        assert not client.posts
        await slack.handle_event(channel_event(channel_type=channel_type))
        await slack.handle_event(followup)
        await slack.handle_event(
            channel_event(
                "3.0", "unrelated", type="message", channel_type=channel_type, thread_ts="other"
            )
        )
        await slack.handle_event(
            channel_event("4.0", "channel chatter", type="message", channel_type=channel_type)
        )
        assert [post["text"] for post in client.posts] == ["inspect <@TARGET>", "and their budget?"]
        assert [post["thread_ts"] for post in client.posts] == ["1.0", "1.0"]
        assert len(client.reactions) == 2

    asyncio.run(scenario())


def test_dual_slack_event_types_share_one_message_identity():
    async def scenario():
        client = FakeSlackClient()
        slack = Slack(bot_token="test", app_token="test", web_client=client, bot_user_id="BOT")
        app = AgentChat(channels=[slack])

        @app.on_message
        async def respond(context):
            return "One action"

        mention = channel_event()
        duplicate = {
            **mention,
            "event_id": "different-delivery",
            "event": {
                **mention["event"],
                "type": "message",
                "channel_type": "channel",
            },
        }
        await slack.handle_event(duplicate)
        await slack.handle_event(mention)
        assert len(client.posts) == 1

    asyncio.run(scenario())


def test_explicit_dm_thread_replies_stay_in_thread_with_shared_dm_context():
    async def scenario():
        client = FakeSlackClient()
        slack = Slack(bot_token="test", app_token="test", web_client=client)
        app = AgentChat(channels=[slack])

        @app.on_message
        async def respond(context):
            return str(len(await context.conversation.history()))

        await slack.handle_event(channel_event(type="message", channel_type="im", text="first"))
        await slack.handle_event(
            channel_event("2.0", "second", type="message", channel_type="im", thread_ts="1.0")
        )
        assert [post["thread_ts"] for post in client.posts] == [None, "1.0"]
        assert [post["text"] for post in client.posts] == ["1", "3"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"bot_id": "another-bot"},
        {"user": "BOT"},
        {"subtype": "message_changed"},
        {"text": "<@BOT>"},
        {"user": ""},
        {"thread_ts": 10},
    ],
)
def test_ignored_events_never_ack_or_reply(change):
    async def scenario():
        client = FakeSlackClient()
        slack = Slack(
            bot_token="test",
            app_token="test",
            web_client=client,
            workspace_id="team-1",
            bot_user_id="BOT",
        )
        app = AgentChat(channels=[slack])

        @app.on_message
        async def respond(context):
            raise AssertionError("Rejected event reached handler")

        await slack.handle_event(channel_event(**change))
        await slack.handle_event({**channel_event(), "team_id": "another-workspace"})
        assert not client.reactions
        assert not client.posts

    asyncio.run(scenario())


def test_socket_acknowledges_next_event_while_handler_is_running():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        received = []
        acknowledgements = []
        slack = Slack(
            bot_token="test",
            app_token="test",
            web_client=FakeSlackClient(),
            bot_user_id="BOT",
            max_pending_events=2,
        )

        async def receive(channel, message):
            started.set()
            await release.wait()
            received.append(message.id)

        class Socket:
            async def send_socket_mode_response(self, response):
                acknowledgements.append(response.envelope_id)

        slack.bind(receive)
        first = SocketModeRequest(type="events_api", envelope_id="one", payload=channel_event())
        second = SocketModeRequest(
            type="events_api", envelope_id="two", payload=channel_event("2.0")
        )
        await slack._handle_socket_request(Socket(), first)
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(slack._handle_socket_request(Socket(), second), timeout=1)
        assert acknowledgements == ["one", "two"]
        assert received == []
        overflow = SocketModeRequest(
            type="events_api", envelope_id="three", payload=channel_event("3.0")
        )
        await slack._handle_socket_request(Socket(), overflow)
        assert acknowledgements == ["one", "two"]
        release.set()
        await slack.close()
        assert len(received) == 2

    asyncio.run(scenario())


def test_startup_verifies_workspace_and_exposes_connection_state(monkeypatch):
    async def scenario():
        connected = asyncio.Event()

        class Client(FakeSlackClient):
            async def auth_test(self):
                return {"team_id": "team-1", "user_id": "BOT"}

        class Socket:
            def __init__(self, **kwargs):
                self.socket_mode_request_listeners = []
                self.connected = False

            async def connect(self):
                self.connected = True
                connected.set()

            async def is_connected(self):
                return self.connected

            async def close(self):
                self.connected = False

        monkeypatch.setattr("agentchat.channels.slack.SocketModeClient", Socket)
        client = Client()
        slack = Slack(bot_token="test", app_token="test", web_client=client, workspace_id="team-1")

        async def receive(channel, message):
            await channel.reply(message, message.text)

        slack.bind(receive)
        assert not await slack.is_connected()
        task = asyncio.create_task(slack.run())
        await asyncio.wait_for(connected.wait(), timeout=1)
        assert await slack.is_connected()
        await slack.handle_event(channel_event(text="<@BOT> hello <@TARGET>"))
        assert client.posts[0]["text"] == "hello <@TARGET>"
        await slack.close()
        await task
        assert not await slack.is_connected()

        wrong_workspace = Slack(
            bot_token="test", app_token="test", web_client=client, workspace_id="other"
        )
        wrong_workspace.bind(receive)
        with pytest.raises(RuntimeError, match="different workspace"):
            await wrong_workspace.run()
        assert not await wrong_workspace.is_connected()

    asyncio.run(scenario())
