import asyncio

from agentchat import AgentChat
from agentchat.channels import Slack
from agentchat.state import MemoryState


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": str(len(self.posts))}


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
            {"channel": "dm-1", "text": "I remember 1 messages", "thread_ts": None},
            {"channel": "dm-1", "text": "I remember 3 messages", "thread_ts": None},
        ]

    asyncio.run(scenario())


def test_slack_mention_replies_in_thread_and_deduplicates() -> None:
    async def scenario() -> None:
        web_client = FakeSlackClient()
        slack = Slack(bot_token="xoxb-test", app_token="xapp-test", web_client=web_client)
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
            {"channel": "channel-1", "text": "hello", "thread_ts": "1.0"}
        ]

    asyncio.run(scenario())
