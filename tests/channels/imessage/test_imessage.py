import asyncio
from urllib.parse import parse_qs

import pytest
import socketio
from aiohttp import web
from aiohttp.test_utils import TestServer

from agentchat import AgentChat
from agentchat.channels.imessage import IMessage
from agentchat.channels.imessage.events import normalize
from agentchat.channels.imessage.transport import BlueBubblesClient
from tests.support.imessage import CHAT_GUID, BubblesClient, SocketClient, imessage_event
from tests.support.receipts import Receipt


@pytest.mark.parametrize(
    "changes",
    [
        {"isFromMe": True},
        {"isFromMe": None},
        {"associatedMessageType": 2000},
        {"isSystemMessage": True},
        {"itemType": 1},
        {"handle": None},
        {"chats": []},
        {"chats": [{"guid": "SMS;-;+15551234567"}]},
        {"text": ""},
        {"chats": [None]},
        {"guid": ""},
        {"attachments": 7, "text": ""},
    ],
)
def test_filters_echoes_reactions_system_sms_and_malformed_events(changes):
    assert normalize(imessage_event(**changes), instance_id="mac") is None


def test_attachment_only_and_group_sender_identity():
    event = imessage_event(
        text="\ufffc",
        chats=[{"guid": "iMessage;+;group"}],
        attachments=[
            {
                "guid": "file",
                "transferName": "photo.jpg",
                "mimeType": "image/jpeg",
                "totalBytes": 50,
            }
        ],
    )
    message = normalize(event, instance_id="mac")
    assert message.sender.id == "+15551234567"
    assert message.text == ""
    assert message.attachments[0].size == 50
    assert message.conversation_id != normalize(imessage_event(), instance_id="mac").conversation_id
    assert message.id != normalize(event, instance_id="other-mac").id


@pytest.mark.asyncio
async def test_allowlist_applies_to_receiving_and_sending():
    receipt = Receipt()
    channel = IMessage(
        server_url="http://localhost:1234",
        password="fake",
        instance_id="mac",
        client=BubblesClient(receipt),
        socket_client=SocketClient(),
        allowed_chat_guids=[],
    )
    seen = []

    async def receive(_, message):
        seen.append(message)

    channel.bind(receive)
    await channel._on_message(imessage_event())
    with pytest.raises(ValueError, match="not allowed"):
        await channel.reply(normalize(imessage_event(), instance_id="mac"), "answer")
    await channel.close()
    assert not seen and not receipt.calls


@pytest.mark.asyncio
async def test_real_socketio_and_rest_round_trip():
    server_socket = socketio.AsyncServer(async_mode="aiohttp")
    server_app = web.Application()
    server_socket.attach(server_app)
    sends = []
    password = "local-test &secret"
    connected, sent = asyncio.Event(), asyncio.Event()

    @server_socket.event
    async def connect(sid, environ, auth):
        assert parse_qs(environ["QUERY_STRING"])["password"] == [password]
        connected.set()

    async def ping(request):
        assert request.query["password"] == password
        return web.json_response({"status": 200, "data": "pong"})

    async def send(request):
        assert request.query["password"] == password
        sends.append(await request.json())
        sent.set()
        return web.json_response({"status": 200, "data": {"guid": "sent-guid", "error": 0}})

    server_app.router.add_get("/api/v1/ping", ping)
    server_app.router.add_post("/api/v1/message/text", send)
    async with TestServer(server_app) as server:
        channel = IMessage(
            server_url=str(server.make_url("/")), password=password, instance_id="mac"
        )
        app = AgentChat(channels=[channel])

        @app.on_message
        async def respond(context):
            return "echo: " + context.message.text

        running = asyncio.create_task(app.run())
        try:
            await asyncio.wait_for(connected.wait(), timeout=3)
            await server_socket.emit("new-message", imessage_event())
            await asyncio.wait_for(sent.wait(), timeout=3)
            await channel.close()
            await asyncio.wait_for(running, timeout=3)
            assert sends[0]["message"] == "echo: hello"
            assert sends[0]["chatGuid"] == CHAT_GUID
            assert sends[0]["method"] == "apple-script"
            assert sends[0]["tempGuid"] != "sent-guid"
            source = normalize(imessage_event(), instance_id="mac")
            history = await app.state.history(source.conversation_id)
            assert len(history) == 2
            assert history[-1].id == "imessage:mac:sent-guid"
            assert channel._client._session.closed
            assert not channel._socket.connected
        finally:
            await app.close()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await server_socket.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,data",
    [
        (401, {"message": "contains-secret"}),
        (302, {}),
        (200, {"status": 200, "data": {"tempGuid": "not-a-receipt"}}),
        (200, {"status": 200, "data": {"guid": "sent", "error": 4}}),
    ],
)
async def test_http_errors_redirects_and_unconfirmed_sends(status, data):
    count = 0

    async def send(request):
        nonlocal count
        count += 1
        return web.json_response(data, status=status, headers={"Location": "/never-follow"})

    server_app = web.Application()
    server_app.router.add_post("/api/v1/message/text", send)
    async with TestServer(server_app) as server:
        client = BlueBubblesClient(str(server.make_url("/")), "contains-secret")
        try:
            with pytest.raises(RuntimeError) as error:
                await client.send_text(CHAT_GUID, "hello")
            assert "contains-secret" not in str(error.value)
            assert count == 1
        finally:
            await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com?password=x",
        "https://example.com/path",
        "ftp://example.com",
    ],
)
def test_url_validation(url):
    with pytest.raises(ValueError):
        BlueBubblesClient(url, "secret")
