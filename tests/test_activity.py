import asyncio
from dataclasses import replace
from types import MappingProxyType

import pytest
from test_app import FakeChannel, incoming
from test_slack import FakeSlackClient, channel_event

from agentchat import AgentChat
from agentchat.channels import Slack


class StatusChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.statuses = []

    async def set_status(self, source, status):
        self.statuses.append((source.conversation_id, status))
        return True


@pytest.mark.asyncio
async def test_status_refresh_update_and_clear_never_enter_history():
    channel = StatusChannel()
    app = AgentChat(channels=[channel])

    @app.on_message
    async def respond(ctx):
        async with ctx.working(refresh_interval=0.01) as activity:
            await activity.update("is testing…")
            await asyncio.sleep(0.025)
            await ctx.reply("Tests passed.")

    await channel.send(incoming("1", "Run tests"))
    labels = [value for _, value in channel.statuses]
    assert labels[0] == "is working…" and labels[-1] == ""
    assert labels.count("is testing…") >= 2
    assert set(labels[1:-1]) == {"is testing…"}
    before = list(channel.statuses)
    await asyncio.sleep(0.025)
    assert channel.statuses == before
    history = await app.state.history("fake:conversation")
    assert [m.text for m in history] == ["Run tests", "Tests passed."]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("agent failed"), asyncio.CancelledError()])
async def test_working_clears_on_failure_and_cancellation(failure):
    channel = StatusChannel()
    app = AgentChat(channels=[channel])

    @app.on_message
    async def respond(ctx):
        async with ctx.working():
            raise failure

    with pytest.raises(type(failure)):
        await channel.send(incoming("1", "Work"))
    assert [status for _, status in channel.statuses] == ["is working…", ""]


@pytest.mark.asyncio
async def test_working_outage_does_not_mask_answer_or_original_error():
    class Unavailable(StatusChannel):
        async def set_status(self, source, status):
            raise TimeoutError("Provider unavailable")

    channel = Unavailable()
    app = AgentChat(channels=[channel])

    @app.on_message
    async def respond(ctx):
        async with ctx.working():
            return "Still answered"

    await channel.send(incoming("1", "Work"))
    assert channel.replies == ["Still answered"]
    with pytest.raises(TimeoutError):
        await app.set_status(channel, incoming("2", "Work"), "is working…")


@pytest.mark.asyncio
async def test_optional_status_capability_is_backwards_compatible():
    channel = FakeChannel()
    app = AgentChat(channels=[channel])

    @app.on_message
    async def respond(ctx):
        assert await ctx.set_status("is working…") is False
        async with ctx.working():
            return "Done"

    await channel.send(incoming("1", "Work"))
    assert channel.replies == ["Done"]


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
