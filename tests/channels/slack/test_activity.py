from dataclasses import replace
from types import MappingProxyType

import pytest

from agentchat import AgentChat
from agentchat.channels.slack import Slack
from tests.support.channels import incoming
from tests.support.slack import FakeSlackClient, channel_event


class SlackStatusClient(FakeSlackClient):
    def __init__(self):
        super().__init__()
        self.statuses = []

    async def assistant_threads_setStatus(self, **kwargs):
        self.statuses.append(kwargs)
        return {"ok": True}


@pytest.mark.asyncio
async def test_slack_native_status_uses_root_and_never_posts_or_reacts():
    client = SlackStatusClient()
    slack = Slack(bot_token="test", app_token="test", web_client=client, ack_emoji=None)
    app = AgentChat(channels=[slack])

    @app.on_message
    async def respond(ctx):
        assert await ctx.set_status("is working…") is True
        assert await ctx.set_status("") is True

    await slack.handle_event(channel_event(ts="3.0", thread_ts="1.0", channel="C123"))
    assert client.statuses == [
        {"channel_id": "C123", "thread_ts": "1.0", "status": "is working…"},
        {"channel_id": "C123", "thread_ts": "1.0", "status": ""},
    ]
    assert client.posts == [] and client.reactions == []


@pytest.mark.asyncio
async def test_plain_dm_status_does_not_open_unexpected_thread():
    client = SlackStatusClient()
    slack = Slack(bot_token="test", app_token="test", web_client=client)
    source = replace(incoming("1", "Hi"), channel="slack",
                     metadata=MappingProxyType({"channel_id": "D123"}))
    assert await slack.set_status(source, "is working…") is False
    source = replace(source, metadata=MappingProxyType({
        "channel_id": "D123", "reply_thread_timestamp": "1.0",
    }))
    assert await slack.set_status(source, "is working…") is True
    assert client.statuses == [{"channel_id": "D123", "thread_ts": "1.0", "status": "is working…"}]


@pytest.mark.asyncio
async def test_status_rejects_another_workspace_before_api_call():
    client = SlackStatusClient()
    slack = Slack(bot_token="test", app_token="test", web_client=client, workspace_id="T123")
    source = replace(incoming("1", "Hi"), channel="slack", metadata=MappingProxyType({
        "team_id": "TOTHER", "channel_id": "C123", "reply_thread_timestamp": "1.0",
    }))
    with pytest.raises(ValueError, match="workspace"):
        await slack.set_status(source, "is working…")
    assert not client.statuses
