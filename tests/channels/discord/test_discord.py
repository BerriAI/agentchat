from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agentchat.channels.discord import Discord
from agentchat.channels.discord.events import normalize
from agentchat.channels.discord.rendering import text_payload
from agentchat.channels.discord.transport import DiscordRESTClient
from tests.support.discord import DiscordClient, discord_event
from tests.support.receipts import Receipt


def test_dm_normalizes_a_real_discord_py_message():
    client = discord.Client(intents=discord.Intents.none())
    state = client._connection
    state.user = discord.ClientUser(
        state=state,
        data={
            "id": "99",
            "username": "Bot",
            "discriminator": "0",
            "bot": True,
            "avatar": None,
        },
    )
    author = {"id": "30", "username": "Alice", "discriminator": "0", "avatar": None}
    channel = discord.DMChannel(
        me=state.user, state=state, data={"id": "10", "recipients": [author]}
    )
    event = discord.Message(
        state=state,
        channel=channel,
        data={
            "id": "100",
            "type": 0,
            "content": "hello",
            "author": author,
            "attachments": [],
            "embeds": [],
            "edited_timestamp": None,
            "pinned": False,
            "mention_everyone": False,
            "tts": False,
            "mentions": [],
        },
    )
    message = normalize(event, bot_id=99)
    assert message.id == "discord:99:10:100"
    assert message.conversation_id == "discord:99:dm:10"
    assert message.sender.display_name == "Alice"
    assert message.thread_id is None


def test_mentions_and_threads_are_scoped_to_provider_channels():
    event = discord_event(
        direct=False,
        thread=True,
        content="<@99> hi <@!99> <@42>",
        mentions=[SimpleNamespace(id=99)],
    )
    message = normalize(event, bot_id=99)
    assert message.text == "hi  <@42>"
    assert message.thread_id == "10"
    assert message.conversation_id == "discord:99:20:10"
    event.channel.id = 11
    assert normalize(event, bot_id=99).conversation_id != message.conversation_id


def test_reply_to_bot_and_attachment_only_dm():
    reference = discord_event(author=SimpleNamespace(id=99, bot=True))
    event = discord_event(direct=False, reference=SimpleNamespace(resolved=reference))
    assert normalize(event, bot_id=99) is not None
    event = discord_event(
        content="",
        attachments=[
            SimpleNamespace(
                id=7,
                filename="picture.png",
                content_type="image/png",
                size=12,
                url="https://example.com/never-retained",
            )
        ],
    )
    message = normalize(event, bot_id=99)
    assert message.attachments[0].id == "7"
    assert "never-retained" not in repr(message)


@pytest.mark.parametrize(
    "changes",
    [
        {"direct": False},
        {"content": ""},
        {"webhook_id": 9},
        {"author": SimpleNamespace(id=99, bot=False)},
        {"author": SimpleNamespace(id=77, bot=True)},
    ],
)
def test_ignores_unaddressed_bot_webhook_and_empty_events(changes):
    assert normalize(discord_event(**changes), bot_id=99) is None


@pytest.mark.asyncio
async def test_reply_disables_mentions_and_uses_message_reference():
    receipt = Receipt(123)
    client = DiscordClient(receipt)
    channel = Discord(bot_token="fake", client=client, send_client=client)
    source = normalize(discord_event(), bot_id=99)
    try:
        await channel.reply(source, "@everyone <@30>")
        destination, options = receipt.calls[0][0]
        assert destination == 10
        assert options["allowed_mentions"] == {"parse": [], "replied_user": False}
        assert options["message_reference"]["message_id"] == "100"
        assert options["message_reference"]["fail_if_not_exists"] is False
    finally:
        await channel.close()


def test_limits_reject_before_network():
    assert text_payload("a" * 2000) == "a" * 2000
    with pytest.raises(ValueError, match="2,000"):
        text_payload("a" * 2001)
    with pytest.raises(ValueError, match="2,000"):
        text_payload("😀" * 1001)


@pytest.mark.asyncio
async def test_login_failure_closes_client():
    client = DiscordClient(Receipt())

    async def fail(token):
        raise discord.LoginFailure("invalid token")

    client.start = fail
    channel = Discord(bot_token="fake", client=client)

    async def receive(*args):
        pass

    channel.bind(receive)
    with pytest.raises(discord.LoginFailure):
        await channel.run()
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429, 500, 302])
async def test_http_send_receipt_and_no_retries(monkeypatch, status):
    calls = []

    async def send(request):
        assert request.headers["Authorization"] == "Bot fake"
        calls.append(await request.json())
        return web.json_response({"id": "123"}, status=status, headers={"Location": "/no-redirect"})

    app = web.Application()
    app.router.add_post("/channels/10/messages", send)
    async with TestServer(app) as server:
        monkeypatch.setattr(
            "agentchat.channels.discord.transport.API_ROOT",
            str(
                server.make_url("/"),
            ).rstrip("/"),
        )
        client = DiscordRESTClient("fake")
        try:
            if status == 200:
                assert await client.send_text(10, {"content": "hello"}) == 123
            else:
                with pytest.raises(RuntimeError, match=f"HTTP {status}"):
                    await client.send_text(10, {"content": "hello"})
            assert len(calls) == 1
        finally:
            await client.close()
