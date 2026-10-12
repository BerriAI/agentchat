from types import SimpleNamespace
from unittest.mock import AsyncMock

from microsoft_agents.activity import ResourceResponse
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import OutboundHostValidator

APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"


def teams_event(*, kind="personal", **changes):
    event = {
        "id": "incoming",
        "type": "message",
        "channelId": "msteams",
        "text": "hello",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "from": {"id": "29:alice", "name": "Alice"},
        "recipient": {"id": f"28:{APP_ID}", "name": "Bot"},
        "conversation": {"id": "conversation", "conversationType": kind},
        "channelData": {"tenant": {"id": TENANT_ID}},
    }
    event.update(changes)
    return event


def teams_adapter(receipt):
    async def send(*args):
        return ResourceResponse.model_construct(id=receipt.get(*args))

    connector = SimpleNamespace(
        conversations=SimpleNamespace(
            send_to_conversation=send,
            reply_to_activity=send,
        ),
        close=AsyncMock(),
    )
    token_client = SimpleNamespace(close=AsyncMock())
    factory = SimpleNamespace(
        create_connector_client=AsyncMock(return_value=connector),
        create_user_token_client=AsyncMock(return_value=token_client),
    )
    adapter = CloudAdapter(
        channel_service_client_factory=factory,
        host_validator=OutboundHostValidator(enabled=True),
    )
    return adapter, connector, factory
