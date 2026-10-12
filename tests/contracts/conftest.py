"""Adapt native event injection to the same contract assertions for each channel."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from agentchat.channels.base import Channel
from agentchat.channels.slack import Slack
from tests.support.channels import FakeChannel, incoming
from tests.support.slack import FakeSlackClient, channel_event


@dataclass
class ChannelHarness:
    channel: Channel
    emit: Callable[[str, str], Awaitable[None]]
    sent_texts: Callable[[], list[str]]
    fail_reply: Callable[[], None]


@pytest.fixture(params=["custom", "slack"])
def channel_harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.param == "custom":
        custom = FakeChannel()

        async def emit_custom(message_id: str, text: str) -> None:
            await custom.send(incoming(f"fake:{message_id}", text))

        def fail_custom() -> None:
            async def reply(source, content):
                raise TimeoutError("Delivery outcome is unknown")

            monkeypatch.setattr(custom, "reply", reply)

        return ChannelHarness(custom, emit_custom, lambda: custom.replies, fail_custom)

    client = FakeSlackClient()
    slack = Slack(
        bot_token="test", app_token="test", web_client=client,
        workspace_id="team-1", bot_user_id="BOT", ack_emoji=None,
    )

    async def emit_slack(message_id: str, text: str) -> None:
        await slack.handle_event(channel_event(
            ts=message_id, text=text, type="message", channel_type="im",
        ))

    def fail_slack() -> None:
        async def post_message(**kwargs):
            raise TimeoutError("Delivery outcome is unknown")

        monkeypatch.setattr(client, "chat_postMessage", post_message)

    return ChannelHarness(
        slack, emit_slack, lambda: [str(post["text"]) for post in client.posts], fail_slack,
    )
