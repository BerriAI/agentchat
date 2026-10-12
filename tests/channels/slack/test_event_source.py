"""The same Slack adapter can consume Socket Mode or host-owned event delivery."""

import asyncio

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from agentchat import AgentChat
from agentchat.channels.slack import Slack
from tests.support.imports import run_isolated
from tests.support.slack import FakeSlackClient, channel_event


class QueueEventSource:
    """A host queue implements the protocol without inheriting library classes."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.connected = False
        self.close_calls = 0

    async def run(self, receiver):
        self.connected = True
        self.started.set()
        try:
            while (payload := await self.queue.get()) is not None:
                await receiver(payload)
        finally:
            self.connected = False

    async def emit(self, payload):
        await self.queue.put(payload)

    async def close(self):
        self.close_calls += 1
        await self.queue.put(None)

    async def is_connected(self):
        return self.connected


class AuthenticatedClient(FakeSlackClient):
    async def auth_test(self):
        return {"team_id": "team-1", "user_id": "BOT"}


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["queue", "socket"])
async def test_sources_share_normalization_subscriptions_history_and_replies(delivery, monkeypatch):
    client = AuthenticatedClient()
    queue_source = QueueEventSource()
    started = queue_source.started
    source_options = {"event_source": queue_source}
    emit = queue_source.emit

    if delivery == "socket":
        started = asyncio.Event()

        class Socket:
            def __init__(self, **kwargs):
                self.socket_mode_request_listeners = []
                self.connected = False

            async def connect(self):
                self.connected = True
                started.set()

            async def close(self):
                self.connected = False

            async def is_connected(self):
                return self.connected

            async def send_socket_mode_response(self, response):
                pass

        socket = Socket()
        monkeypatch.setattr(
            "agentchat.channels.slack.socket_mode.SocketModeClient", lambda **kwargs: socket,
        )
        source_options = {"app_token": "xapp-test"}

        async def emit(payload):
            request = SocketModeRequest(
                type="events_api", envelope_id=payload["event_id"], payload=payload,
            )
            await socket.socket_mode_request_listeners[0](socket, request)

    slack = Slack(bot_token="xoxb-test", web_client=client, **source_options)
    processed = asyncio.Queue()
    handle_event = slack.handle_event

    async def track_event(payload):
        await handle_event(payload)
        await processed.put(payload["event_id"])

    monkeypatch.setattr(slack, "handle_event", track_event)
    app = AgentChat(channels=[slack])
    received = []

    @app.on_message
    async def respond(context):
        received.append(context.message)
        await slack.subscribe(context.message)
        history = await context.conversation.history()
        return f"turn {len(history)}: {context.message.text}"

    async def deliver(payload):
        await emit(payload)
        assert await asyncio.wait_for(processed.get(), timeout=1) == payload["event_id"]

    assert not await slack.is_connected()
    run = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await slack.is_connected()
        followup = channel_event(
            "2.0", "more detail", type="message", channel_type="channel", thread_ts="1.0",
        )
        await deliver(followup)
        assert not client.posts
        await deliver(channel_event(text="<@BOT> hello <@TARGET>"))
        await deliver(channel_event(text="<@BOT> hello <@TARGET>"))
        await deliver(followup)
        await deliver(channel_event(
            "3.0", "unrelated", type="message", channel_type="channel", thread_ts="other",
        ))

        assert [message.text for message in received] == ["hello <@TARGET>", "more detail"]
        assert [message.addressed for message in received] == [True, False]
        assert [post["text"] for post in client.posts] == [
            "turn 1: hello <@TARGET>", "turn 3: more detail",
        ]
        assert [post["thread_ts"] for post in client.posts] == ["1.0", "1.0"]
        assert {post["channel"] for post in client.posts} == {"channel-1"}
        history = await app.state.history(received[0].conversation_id)
        assert [(message.role, message.text) for message in history] == [
            ("user", "hello <@TARGET>"), ("assistant", "turn 1: hello <@TARGET>"),
            ("user", "more detail"), ("assistant", "turn 3: more detail"),
        ]
    finally:
        await app.close()
        await asyncio.wait_for(run, timeout=1)

    assert not await slack.is_connected()
    if delivery == "queue":
        assert queue_source.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity, workspace_id, error", [
    ({"team_id": "team-1", "user_id": "BOT"}, "other", "different workspace"),
    ({"team_id": "team-1"}, None, "bot identity"),
    ({"team_id": 123, "user_id": "BOT"}, None, "bot identity"),
])
async def test_authentication_rejects_invalid_identity_before_starting_source(
    identity, workspace_id, error,
):
    class Client(FakeSlackClient):
        async def auth_test(self):
            return identity

    source = QueueEventSource()
    slack = Slack(
        bot_token="test", event_source=source, web_client=Client(), workspace_id=workspace_id,
    )

    async def receive(channel, message):
        raise AssertionError("An unauthenticated source must not deliver messages")

    slack.bind(receive)
    with pytest.raises(RuntimeError, match=error):
        await slack.run()
    assert not source.started.is_set()
    assert not await slack.is_connected()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["close", "cancel_run"])
async def test_stopping_during_authentication_cancels_startup_without_starting_source(shutdown):
    authenticating, cancelled = asyncio.Event(), asyncio.Event()

    class Client(FakeSlackClient):
        async def auth_test(self):
            authenticating.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    source = QueueEventSource()
    slack = Slack(bot_token="test", event_source=source, web_client=Client())

    async def receive(channel, message):
        raise AssertionError("A closed adapter must not deliver messages")

    slack.bind(receive)
    run = asyncio.create_task(slack.run())
    try:
        await asyncio.wait_for(authenticating.wait(), timeout=1)
        if shutdown == "close":
            await asyncio.wait_for(slack.close(), timeout=1)
        else:
            run.cancel()
        done, pending = await asyncio.wait({run}, timeout=1)
        assert done == {run} and not pending
        if shutdown == "close":
            await run
        else:
            with pytest.raises(asyncio.CancelledError):
                await run
        assert cancelled.is_set()
        assert not source.started.is_set()
        assert source.close_calls == 1
        await slack.close()
        assert source.close_calls == 1
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_authentication_failure_closes_source_and_preserves_native_error():
    refusal = ConnectionError("Slack authentication unavailable")

    class Client(FakeSlackClient):
        async def auth_test(self):
            raise refusal

    source = QueueEventSource()
    slack = Slack(bot_token="test", event_source=source, web_client=Client())

    async def receive(channel, message):
        raise AssertionError("An unauthenticated adapter must not deliver messages")

    slack.bind(receive)
    with pytest.raises(ConnectionError) as caught:
        await slack.run()
    assert caught.value is refusal
    assert not source.started.is_set()
    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_slack_run_cancellation_closes_the_host_source_and_pending_delivery():
    source = QueueEventSource()
    slack = Slack(bot_token="test", event_source=source, web_client=AuthenticatedClient())
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def receive(channel, message):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    slack.bind(receive)
    run = asyncio.create_task(slack.run())
    await asyncio.wait_for(source.started.wait(), timeout=1)
    await source.emit(channel_event())
    await asyncio.wait_for(started.wait(), timeout=1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=1)
    assert cancelled.is_set()
    assert not await slack.is_connected()
    assert source.close_calls == 1
    await slack.close()
    assert source.close_calls == 1


def test_slack_requires_exactly_one_event_source():
    with pytest.raises(ValueError, match=r"app_token|event_source"):
        Slack(bot_token="test", web_client=FakeSlackClient())
    with pytest.raises(ValueError, match=r"app_token|event_source"):
        Slack(
            bot_token="test", app_token="test", event_source=QueueEventSource(),
            web_client=FakeSlackClient(),
        )


def test_custom_source_does_not_import_socket_mode():
    run_isolated("""
        import importlib.abc
        import sys

        class BlockSocketMode(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith((
                    "slack_sdk.socket_mode", "agentchat.channels.slack.socket_mode",
                )):
                    raise AssertionError("A custom source must not load Socket Mode")

        sys.meta_path.insert(0, BlockSocketMode())
        from agentchat.channels.slack import Slack

        class Source:
            async def run(self, receiver): pass
            async def close(self): pass
            async def is_connected(self): return False

        Slack(bot_token="test", event_source=Source())
        assert not any(name.startswith("slack_sdk.socket_mode") for name in sys.modules)
    """)


def test_slack_payload_helpers_and_event_source_contract_do_not_import_sdks():
    run_isolated("""
        import importlib.abc
        import sys

        blocked = {"slack_sdk", "aiohttp"}

        class BlockSDKs(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in blocked:
                    raise AssertionError("Pure Slack modules must not load provider SDKs")

        sys.meta_path.insert(0, BlockSDKs())
        from agentchat.channels.slack import events, mirroring, rendering, transport

        assert callable(events.to_message)
        assert callable(mirroring.mirror_payload)
        assert callable(rendering.text_blocks)
        assert transport.SlackEventSource
        assert not blocked.intersection(name.split(".")[0] for name in sys.modules)
    """)
