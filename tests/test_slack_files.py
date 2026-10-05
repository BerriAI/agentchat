import asyncio
from dataclasses import replace

import pytest
from test_app import FakeChannel
from test_slack import FakeSlackClient, channel_event

from agentchat import AgentChat, Attachment, DownloadedFile, Message, Sender
from agentchat.channels import Slack, slack_files


def transport(monkeypatch, *, chunks=(b"image",), status=200):
    calls = []

    class Response:
        def __init__(self):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            for chunk in chunks:
                yield chunk

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 30

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(slack_files.aiohttp, "ClientSession", Session)
    return calls


def metadata(**overrides):
    return {"id": "F12345678", "name": "image.png", "mimetype": "image/png", "size": 5,
            "url_private_download": "https://files.slack.com/files-pri/T123-F12345678/image.png",
            **overrides}


def test_parser_keeps_only_bounded_unique_file_references():
    files = [metadata(), metadata(), {"id": "../token"}, {"id": "F2"}, {"id": "F3"}]
    result = slack_files.slack_attachments(files, limit=4)
    assert result == (Attachment("F12345678", "image.png", "image/png", 5), Attachment("F2"))
    assert "url_private" not in repr(result)
    assert slack_files.slack_attachments(None) == ()
    assert slack_files.slack_attachments([None, {"id": 4}]) == ()


@pytest.mark.asyncio
async def test_download_uses_fresh_metadata_and_keeps_bytes_out_of_history(monkeypatch):
    calls = transport(monkeypatch)
    requests = []

    class Client(FakeSlackClient):
        async def api_call(self, api, **kwargs):
            requests.append((api, kwargs))
            return {"ok": True, "file": metadata()}

    slack = Slack(bot_token="bot-only", app_token="socket-only", web_client=Client(),
                  workspace_id="team-1", bot_user_id="BOT", ack_emoji=None)
    app = AgentChat(channels=[slack])
    received = []

    @app.on_message
    async def handle(context):
        received.append(context.message)
        downloaded = await context.download_attachment(context.message.attachments[0], max_bytes=10)
        assert downloaded == DownloadedFile("F12345678", "image.png", b"image", "image/png")
        assert "image'" not in repr(downloaded)

    event = channel_event(text="<@BOT>", subtype="file_share", files=[{
        **metadata(name="stale.png", size=9999), "url_private": "https://evil.example/token",
    }])
    await slack.handle_event(event)
    # Paired mention/message delivery invokes the handler only once.
    event["event"].update(type="message", channel_type="channel")
    await slack.handle_event(event)
    assert len(received) == 1 and received[0].text == ""
    assert len(received[0].attachments) == 1
    history = await app.state.history(received[0].conversation_id)
    assert history == tuple(received)
    assert requests == [("files.info", {"http_verb": "GET", "params": {"file": "F12345678"}})]
    assert calls == [(metadata()["url_private_download"], {
        "headers": {"Authorization": "Bearer bot-only"}, "allow_redirects": False,
    })]


@pytest.mark.asyncio
async def test_file_only_dms_and_subscribed_replies_preserve_routing_without_downloads():
    slack = Slack(bot_token="test", app_token="test", web_client=object(),
                  workspace_id="team-1", bot_user_id="BOT", ack_emoji=None)
    messages = []

    async def receive(channel, message):
        messages.append(message)

    slack.bind(receive)
    await slack.handle_event(channel_event(text="", type="message", channel_type="im",
                                           subtype="file_share", files=[metadata()]))
    assert messages[-1].conversation_id == "slack:team-1:channel-1"
    await slack.handle_event(channel_event("2.0", text="<@BOT> inspect"))
    root = messages[-1]
    reply = channel_event("3.0", text="", type="message", channel_type="channel",
                          thread_ts="2.0", subtype="file_share", files=[metadata()])
    await slack.handle_event(reply)
    assert len(messages) == 2  # The host has not subscribed to this thread yet.
    await slack.subscribe(root)
    await slack.handle_event(reply)
    assert len(messages) == 3 and not messages[-1].addressed
    assert messages[-1].conversation_id == root.conversation_id
    assert messages[-1].attachments[0].id == "F12345678"
    for fields in ({"bot_id": "B1"}, {"bot_profile": {}}, {"subtype": "message_changed"}):
        await slack.handle_event({**reply, "event": {**reply["event"], **fields}})
    await slack.handle_event({**reply, "team_id": "TOTHER"})
    await slack.handle_event(channel_event("4.0", text="", type="message",
                                           channel_type="channel", files=[metadata()]))
    assert len(messages) == 3


@pytest.mark.asyncio
async def test_download_requires_source_membership_and_workspace_before_network(monkeypatch):
    calls = transport(monkeypatch)
    attachment = Attachment("F12345678")
    source = Message("1", "conversation", "slack", Sender("user"), "", "user",
                     metadata={"team_id": "TOTHER"}, attachments=(attachment,))
    slack = Slack(bot_token="test", app_token="test", web_client=object(), workspace_id="T123")
    with pytest.raises(ValueError, match="workspace"):
        await slack.download_attachment(source, attachment)
    source = replace(source, metadata={"team_id": "T123"})
    with pytest.raises(ValueError, match="source message"):
        await slack.download_attachment(source, Attachment("F999"))
    channel = FakeChannel()
    app = AgentChat(channels=[channel])
    with pytest.raises(TypeError, match="does not support"):
        await app.download_attachment(channel, source, attachment)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,error", [
    ({"id": "FOTHER"}, "unavailable"), ({"is_external": True}, "unavailable"),
    ({"size": 11}, "download limit"), ({"size": True}, "invalid file size"),
    ({"url_private_download": "https://evil.example/image.png"}, "unsupported file location"),
    ({"url_private_download": "https://files.slack.com.evil.example/files-pri/x"}, "location"),
    ({"url_private_download": "http://files.slack.com/files-pri/x"}, "location"),
    ({"url_private_download": "https://user:secret@files.slack.com/files-pri/x"}, "location"),
    ({"url_private_download": "https://files.slack.com:444/files-pri/x"}, "location"),
    ({"url_private_download": "https://files.slack.com:bad/files-pri/x"}, "location"),
    ({"url_private_download": "https://files.slack.com/files-pri/x#fragment"}, "location"),
    ({"url_private_download": "https://files.slack.com/other/x"}, "location"),
])
async def test_invalid_metadata_fails_before_downloading(monkeypatch, overrides, error):
    calls = transport(monkeypatch)

    async def request(api, payload):
        return {"ok": True, "file": metadata(**overrides)}

    with pytest.raises(ValueError, match=error):
        await slack_files.download_slack_file(Attachment("F12345678"), bot_token="secret",
                                              request=request, max_bytes=10)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks,status,error", [
    ((b"12345", b"678901"), 200, "download limit"),
    ((), 200, "empty"), ((b"image",), 302, "Could not read"),
    ((b"image",), 403, "Could not read"),
])
async def test_download_bounds_actual_bytes_and_never_follows_redirects(
    monkeypatch, chunks, status, error,
):
    calls = transport(monkeypatch, chunks=chunks, status=status)

    async def request(api, payload):
        return {"ok": True, "file": metadata(size=1)}

    with pytest.raises((ValueError, RuntimeError), match=error):
        await slack_files.download_slack_file(Attachment("F12345678"), bot_token="secret",
                                              request=request, max_bytes=10)
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", range(3))
async def test_access_revocation_stops_each_download_boundary(monkeypatch, boundary):
    calls = transport(monkeypatch)
    requests, checks = [], []

    async def request(api, payload):
        requests.append(api)
        return {"ok": True, "file": metadata()}

    async def guard():
        checks.append(1)
        if len(checks) == boundary + 1:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await slack_files.download_slack_file(Attachment("F12345678"), bot_token="secret",
                                              request=request, before_download=guard)
    assert len(requests) == (boundary > 0)
    assert len(calls) == (boundary > 1)


@pytest.mark.asyncio
async def test_thread_history_keeps_file_only_messages_and_downloadable_sources():
    class Client:
        async def conversations_replies(self, **kwargs):
            return {"ok": True, "messages": [
                {"ts": "1.0", "user": "user", "files": [metadata()]},
                {"ts": "2.0", "user": "user", "text": "Analyze this"},
            ]}

    slack = Slack(bot_token="test", app_token="test", web_client=Client(), workspace_id="team-1")
    source = Message("1", "conversation", "slack", Sender("user"), "Analyze this", "user",
                     metadata={"team_id": "team-1", "channel_id": "channel-1",
                               "reply_thread_timestamp": "1.0", "message_timestamp": "2.0"})
    history = await slack.thread_history(source)
    assert len(history) == 1 and history[0].text == ""
    assert history[0].attachments[0].id == "F12345678"
    assert history[0].metadata["team_id"] == "team-1"
