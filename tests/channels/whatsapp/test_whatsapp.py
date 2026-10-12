import asyncio
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentchat import AgentChat
from agentchat.channels.whatsapp import WhatsApp
from agentchat.channels.whatsapp.events import normalize
from agentchat.channels.whatsapp.rendering import text_payload
from tests.support.receipts import Receipt
from tests.support.whatsapp import WhatsAppSession, whatsapp_event


def make_channel(**options):
    return WhatsApp(
        phone_number_id="123",
        access_token="token",
        app_secret="secret",
        verify_token="verify",
        **options,
    )


def signed(payload):
    raw = json.dumps(payload).encode()
    signature = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    return {"data": raw, "headers": {"X-Hub-Signature-256": f"sha256={signature}"}}


@pytest.mark.asyncio
async def test_verification_and_rejects_unsigned_or_tampered_webhooks():
    channel = make_channel()
    try:
        async with TestClient(TestServer(channel.create_app())) as client:
            response = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "verify",
                    "hub.challenge": "12345",
                },
            )
            assert response.status == 200 and await response.text() == "12345"
            response = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong",
                    "hub.challenge": "12345",
                },
            )
            assert response.status == 403
            response = await client.post("/webhooks/whatsapp", json=whatsapp_event())
            assert response.status == 401
            payload = signed(whatsapp_event())
            payload["data"] += b" "
            response = await client.post("/webhooks/whatsapp", **payload)
            assert response.status == 401
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_real_webhook_to_graph_round_trip_and_duplicate_claim(monkeypatch):
    sends = []
    sent = asyncio.Event()

    async def send(request):
        assert request.headers["Authorization"] == "Bearer token"
        sends.append(await request.json())
        sent.set()
        return web.json_response(
            {"messaging_product": "whatsapp", "messages": [{"id": "wamid.sent"}]}
        )

    graph = web.Application()
    graph.router.add_post("/v23.0/123/messages", send)
    async with TestServer(graph) as graph_server:
        monkeypatch.setattr(
            "agentchat.channels.whatsapp.adapter.GRAPH_ROOT",
            str(
                graph_server.make_url("/"),
            ).rstrip("/"),
        )
        channel = make_channel()
        app = AgentChat(channels=[channel])

        @app.on_message
        async def respond(context):
            return "echo: " + context.message.text

        try:
            async with TestClient(TestServer(channel.create_app())) as client:
                response = await client.post("/webhooks/whatsapp", **signed(whatsapp_event()))
                assert response.status == 200
                await asyncio.wait_for(sent.wait(), timeout=2)
                response = await client.post("/webhooks/whatsapp", **signed(whatsapp_event()))
                assert response.status == 200
                await app.close()
                assert len(sends) == 1
                assert sends[0]["context"] == {"message_id": "wamid.incoming"}
                assert sends[0]["to"] == "15551234567"
                assert sends[0]["text"] == {"body": "echo: hello", "preview_url": False}
                history = await app.state.history("whatsapp:123:15551234567")
                assert history[-1].id == "whatsapp:123:wamid.sent"
        finally:
            await app.close()


@pytest.mark.asyncio
async def test_batch_overload_never_partially_accepts():
    channel = make_channel(max_pending_events=1)
    seen = []

    async def receive(*args):
        seen.append(args)

    channel.bind(receive)
    messages = [
        {"id": f"wamid.{i}", "from": "15551234567", "type": "text", "text": {"body": "hello"}}
        for i in range(2)
    ]
    try:
        async with TestClient(TestServer(channel.create_app())) as client:
            response = await client.post(
                "/webhooks/whatsapp", **signed(whatsapp_event(messages=messages))
            )
            assert response.status == 503
            assert not seen and not channel._delivery.tasks
    finally:
        await channel.close()


def test_batches_media_button_inputs_and_account_isolation():
    messages = [
        {
            "id": "image",
            "from": "15551234567",
            "type": "image",
            "image": {"id": "media-id", "mime_type": "image/png"},
        },
        {
            "id": "button",
            "from": "15557654321",
            "type": "interactive",
            "interactive": {"button_reply": {"id": "yes", "title": "Yes"}},
        },
        {"id": "reaction", "from": "15551234567", "type": "reaction"},
    ]
    output = normalize(whatsapp_event(messages=messages), phone_number_id="123")
    assert len(output) == 2
    assert output[0].attachments[0].id == "media-id"
    assert output[1].text == "Yes"
    assert output[0].conversation_id != output[1].conversation_id
    assert output[0].sender.display_name == "Alice"
    assert output[1].sender.display_name is None
    assert normalize(whatsapp_event(), phone_number_id="other") == ()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"object": "wrong"},
        {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [None]}],
        },
    ],
)
def test_malformed_and_status_only_events_are_ignored(payload):
    assert normalize(payload, phone_number_id="123") == ()


def test_whatsapp_text_limit():
    assert text_payload("a" * 4096, recipient="123", message_id="m")["type"] == "text"
    with pytest.raises(ValueError, match="4,096"):
        text_payload("a" * 4097, recipient="123", message_id="m")


@pytest.mark.asyncio
async def test_shutdown_drains_handler_before_first_http_session_is_created(monkeypatch):
    receipt = Receipt()
    session = WhatsAppSession(receipt)
    monkeypatch.setattr(
        "agentchat.channels.whatsapp.adapter.aiohttp.ClientSession", lambda **kw: session
    )
    channel = make_channel()

    async def receive(channel, source):
        await channel.reply(source, "final reply")

    channel.bind(receive)
    channel._delivery.submit(channel, normalize(whatsapp_event(), phone_number_id="123")[0])
    await channel.close()
    assert len(receipt.calls) == 1
    assert session.closed
