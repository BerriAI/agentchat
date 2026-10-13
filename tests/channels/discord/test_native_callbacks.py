"""Native callbacks preserve Python SDK discovery and cyclic garbage collection."""
import gc
import inspect
import weakref

import pytest

from agentchat.channels.discord import Discord
from agentchat.channels.imessage import IMessage
from tests.support.discord import DiscordClient
from tests.support.imessage import BubblesClient, SocketClient
from tests.support.receipts import Receipt


@pytest.mark.asyncio
async def test_discord_callback_cycle_can_be_collected():
    client = DiscordClient(Receipt(123))
    reference = weakref.ref(client)
    channel = Discord(bot_token="fake", client=client, send_client=client)
    assert inspect.iscoroutinefunction(client.on_message)
    await channel.close()
    del channel, client
    gc.collect()
    assert reference() is None


@pytest.mark.asyncio
async def test_imessage_callback_is_inspectable_and_collectible():
    socket = SocketClient()
    reference = weakref.ref(socket)
    channel = IMessage(
        server_url="http://localhost:1234",
        password="fake",
        client=BubblesClient(Receipt()),
        socket_client=socket,
    )
    assert inspect.iscoroutinefunction(socket.callback)
    await channel.close()
    del channel, socket
    gc.collect()
    assert reference() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["discord", "imessage", "teams"])
async def test_provider_receipts_retain_opaque_and_cyclic_metadata(provider):
    from dataclasses import replace

    from agentchat.channels.discord.events import normalize as discord_message
    from agentchat.channels.imessage.events import normalize as imessage_message
    from agentchat.channels.teams import Teams
    from agentchat.channels.teams.events import normalize as teams_message
    from tests.support.discord import discord_event
    from tests.support.imessage import imessage_event
    from tests.support.teams import APP_ID, TENANT_ID, teams_adapter, teams_event

    if provider == "discord":
        client = DiscordClient(Receipt(123))
        channel = Discord(bot_token="fake", client=client, send_client=client)
        source = discord_message(discord_event(), bot_id=99)
    elif provider == "imessage":
        channel = IMessage(
            server_url="http://localhost:1234", password="fake", instance_id="mac",
            client=BubblesClient(Receipt("sent")), socket_client=SocketClient(),
        )
        source = imessage_message(imessage_event(), instance_id="mac")
    else:
        adapter, _, _ = teams_adapter(Receipt("sent"))
        channel = Teams(
            app_id=APP_ID, app_password="fake", tenant_id=TENANT_ID, adapter=adapter,
        )
        source = teams_message(teams_event(), app_id=APP_ID, tenant_id=TENANT_ID)
    opaque = object()
    cyclic = []
    cyclic.append(cyclic)
    source = replace(source, metadata={**source.metadata, "opaque": opaque, "cyclic": cyclic})
    try:
        reply = await channel.reply(source, "answer")
        assert reply.metadata["opaque"] is opaque
        assert reply.metadata["cyclic"] is cyclic
        assert reply.id != source.id
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_discord_subclass_callbacks_and_weakrefs():
    class CustomDiscord(Discord):
        async def _on_message(self, event):
            self.last_event = event

    client = DiscordClient(Receipt(123))
    channel = CustomDiscord(bot_token="fake", client=client, send_client=client)
    reference = weakref.ref(channel)
    event = object()
    await client.on_message(event)
    assert type(channel) is CustomDiscord
    assert channel.last_event is event
    assert reference() is channel
    await channel.close()


@pytest.mark.asyncio
async def test_imessage_subclass_callbacks_and_weakrefs():
    class CustomIMessage(IMessage):
        async def _on_message(self, event):
            self.last_event = event

    socket = SocketClient()
    channel = CustomIMessage(
        server_url="http://localhost:1234", password="fake",
        client=BubblesClient(Receipt()), socket_client=socket,
    )
    reference = weakref.ref(channel)
    event = object()
    await socket.callback(event)
    assert type(channel) is CustomIMessage
    assert channel.last_event is event
    assert reference() is channel
    await channel.close()


@pytest.mark.asyncio
async def test_teams_subclass_callbacks_and_weakrefs():
    from agentchat.channels.teams import Teams
    from tests.support.teams import APP_ID, TENANT_ID, teams_adapter

    class CustomTeams(Teams):
        async def _turn_error(self, context, error):
            self.last_error = error

    adapter, _, _ = teams_adapter(Receipt())
    channel = CustomTeams(
        app_id=APP_ID, app_password="fake", tenant_id=TENANT_ID, adapter=adapter,
    )
    reference = weakref.ref(channel)
    error = ValueError("local")
    await adapter.on_turn_error(None, error)
    assert type(channel) is CustomTeams
    assert channel.last_error is error
    assert reference() is channel
    await channel.close()


def test_from_env_preserves_subclass_construction(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")

    class RecordingDiscord(Discord):
        def __init__(self, **options):
            self.options = options

    instance = RecordingDiscord.from_env()
    assert type(instance) is RecordingDiscord
    assert instance.options == {"bot_token": "fake"}


@pytest.mark.asyncio
async def test_rest_clients_support_subclasses_and_weakrefs():
    from agentchat.channels.discord.transport import DiscordRESTClient
    from agentchat.channels.imessage.transport import BlueBubblesClient

    class CustomDiscordREST(DiscordRESTClient):
        pass

    class CustomBlueBubbles(BlueBubblesClient):
        pass

    discord_client = CustomDiscordREST("fake")
    bubbles_client = CustomBlueBubbles("http://localhost:1234", "fake")
    assert type(discord_client) is CustomDiscordREST
    assert type(bubbles_client) is CustomBlueBubbles
    assert weakref.ref(discord_client)() is discord_client
    assert weakref.ref(bubbles_client)() is bubbles_client
    await discord_client.close()
    await bubbles_client.close()


@pytest.mark.parametrize("provider", ["discord", "imessage"])
def test_rest_client_session_cycles_are_collectible(provider):
    from types import SimpleNamespace

    from agentchat.channels.discord.transport import DiscordRESTClient
    from agentchat.channels.imessage.transport import BlueBubblesClient

    client = (
        DiscordRESTClient("fake") if provider == "discord"
        else BlueBubblesClient("http://localhost:1234", "fake")
    )
    reference = weakref.ref(client)
    client._session = SimpleNamespace(client=client)
    del client
    gc.collect()
    assert reference() is None


@pytest.mark.asyncio
async def test_rest_client_subclasses_can_adapt_constructor_arguments():
    from agentchat.channels.discord.transport import DiscordRESTClient
    from agentchat.channels.imessage.transport import BlueBubblesClient

    class ConfiguredDiscord(DiscordRESTClient):
        def __init__(self, config):
            super().__init__(bot_token=config["token"])

    class ConfiguredBlueBubbles(BlueBubblesClient):
        def __init__(self, config):
            super().__init__(server_url=config["url"], password=config["password"])

    discord_client = ConfiguredDiscord({"token": "fake"})
    bubbles_client = ConfiguredBlueBubbles({"url": "http://localhost:1234", "password": "fake"})
    assert isinstance(discord_client, ConfiguredDiscord)
    assert bubbles_client.server_url == "http://localhost:1234"
    await discord_client.close()
    await bubbles_client.close()
