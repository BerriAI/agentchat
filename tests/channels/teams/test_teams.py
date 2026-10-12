import asyncio
import importlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import ClaimsIdentity

from agentchat.channels.teams import Teams
from agentchat.channels.teams.events import normalize
from tests.support.receipts import Receipt
from tests.support.teams import APP_ID, TENANT_ID, teams_adapter, teams_event


def make_channel(**options):
    receipt = Receipt()
    adapter, connector, factory = teams_adapter(receipt)
    channel = Teams(
        app_id=APP_ID,
        tenant_id=TENANT_ID,
        app_password="fake",
        adapter=adapter,
        **options,
    )
    return channel, receipt, connector, factory


def mention():
    return {"type": "mention", "text": "<at>Bot</at>", "mentioned": {"id": f"28:{APP_ID}"}}


@pytest.mark.parametrize("kind", ["personal", "groupChat", "channel"])
@pytest.mark.asyncio
async def test_actual_microsoft_sdk_reply_routing(kind):
    channel, receipt, connector, _ = make_channel()
    event = teams_event(kind=kind, text="<at>Bot</at> hello", entities=[mention()])
    if kind == "channel":
        event["replyToId"] = "root"
    source = normalize(event, app_id=APP_ID, tenant_id=TENANT_ID)
    try:
        response = await channel.reply(source, "answer")
        args, _ = receipt.calls[0]
        sent = args[-1]
        assert sent.conversation.id == "conversation"
        assert sent.from_property.id == f"28:{APP_ID}"
        assert sent.recipient.id == "29:alice"
        assert sent.reply_to_id == ("root" if kind == "channel" else None)
        assert sent.text_format == "plain"
        assert response.thread_id == source.thread_id
        connector.close.assert_awaited_once()
    finally:
        await channel.close()


def test_channel_roots_and_mentions_preserve_identity():
    first = teams_event(kind="channel", id="root", entities=[mention()])
    reply = teams_event(kind="channel", id="reply", replyToId="root", entities=[mention()])
    reply["conversation"]["id"] += ";messageid=root"
    one = normalize(first, app_id=APP_ID, tenant_id=TENANT_ID)
    two = normalize(reply, app_id=APP_ID, tenant_id=TENANT_ID)
    assert one.conversation_id == two.conversation_id
    assert one.id != two.id
    assert one.thread_id == two.thread_id == "root"
    reply["replyToId"] = "other-root"
    assert (
        normalize(reply, app_id=APP_ID, tenant_id=TENANT_ID).conversation_id != one.conversation_id
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "typing"},
        {"channelId": "webchat"},
        {"kind": "channel"},
        {"channelData": {"tenant": {"id": "other"}}},
        {"channelData": {"tenant": None}},
        {"recipient": {"id": "other-bot"}},
        {"from": {"id": f"28:{APP_ID}"}},
        {"conversation": {"id": "c", "conversationType": "unknown"}},
        {"text": ""},
    ],
)
def test_ignores_nonmessages_wrong_tenant_and_unaddressed_group_events(changes):
    assert normalize(teams_event(**changes), app_id=APP_ID, tenant_id=TENANT_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Basic x", "Bearer invalid"])
async def test_endpoint_rejects_unauthenticated_requests(authorization):
    channel, receipt, _, _ = make_channel()
    async with TestClient(TestServer(channel.create_app())) as client:
        headers = {"Authorization": authorization} if authorization else {}
        response = await client.post("/api/messages", json=teams_event(), headers=headers)
        assert response.status == 401
        assert not receipt.calls
    await channel.close()


@pytest.mark.asyncio
async def test_authenticated_endpoint_returns_before_handler_and_backpressures(monkeypatch):
    channel, _, _, _ = make_channel(max_pending_events=1, shutdown_timeout=0)
    started = asyncio.Event()

    async def receive(*args):
        started.set()
        await asyncio.Event().wait()

    channel.bind(receive)
    # Stub the validated identity at the JWT boundary; exercise the real CloudAdapter below it.
    middleware = importlib.import_module(
        "microsoft_agents.hosting.aiohttp.jwt_authorization_middleware",
    )
    monkeypatch.setattr(
        middleware,
        "_authorize_request",
        AsyncMock(
            return_value=ClaimsIdentity(
                {"aud": APP_ID, "iss": "https://api.botframework.com"},
                "JWT",
            )
        ),
    )
    try:
        async with TestClient(TestServer(channel.create_app())) as client:
            response = await client.post("/api/messages", json=teams_event())
            assert response.status == 202
            await asyncio.wait_for(started.wait(), timeout=1)
            response = await client.post("/api/messages", json=teams_event(id="second"))
            assert response.status == 503
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_https_url_is_checked_before_sdk_allocates_connector(monkeypatch):
    channel, _, _, factory = make_channel()
    middleware = importlib.import_module(
        "microsoft_agents.hosting.aiohttp.jwt_authorization_middleware",
    )
    monkeypatch.setattr(
        middleware,
        "_authorize_request",
        AsyncMock(
            return_value=ClaimsIdentity(
                {"aud": APP_ID, "iss": "https://api.botframework.com"},
                "JWT",
            )
        ),
    )
    try:
        async with TestClient(TestServer(channel.create_app())) as client:
            response = await client.post(
                "/api/messages",
                json=teams_event(
                    serviceUrl="http://smba.trafficmanager.net/amer/",
                ),
            )
            assert response.status == 400
            factory.create_connector_client.assert_not_called()
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://smba.trafficmanager.net/",
        "https://evil.example/",
        "https://smba.trafficmanager.net.evil.example/",
        "https://localhost/",
        "https://user:password@smba.trafficmanager.net/",
    ],
)
async def test_refuses_unsafe_service_urls(url):
    channel, receipt, _, _ = make_channel()
    source = normalize(teams_event(serviceUrl=url), app_id=APP_ID, tenant_id=TENANT_ID)
    try:
        with pytest.raises(ValueError, match="HTTPS Microsoft"):
            await channel.reply(source, "answer")
        assert not receipt.calls
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_on_turn_normalization_uses_sdk_aliases_and_missing_receipt_is_error():
    channel, _, _, _ = make_channel()
    seen = []

    async def receive(_, message):
        seen.append(message)

    channel.bind(receive)
    activity = Activity.model_validate(teams_event())
    await channel.on_turn(SimpleNamespace(activity=activity))
    await channel.close()
    assert seen[0].text == "hello"
    with pytest.raises(ValueError):
        await channel.reply(replace(seen[0], metadata={}), "answer")
