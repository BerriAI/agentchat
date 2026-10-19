import pytest

from agentchat.channels.slack.rendering import message_request
from agentchat.models import RichReply


def test_request_routes_a_rendered_reply_without_sending():
    metadata = {"event_type": "saved_delivery", "event_payload": {"id": "one"}}
    request = message_request(
        RichReply("Ready"), channel_id="C123", thread_ts="123.001", metadata=metadata,
    )
    assert request["method"] == "chat.postMessage"
    payload = request["payload"]
    assert payload["channel"] == "C123" and payload["thread_ts"] == "123.001"
    assert payload["metadata"] == metadata
    assert payload["blocks"][0]["expand"] is True
    assert payload["parse"] == "none" and payload["link_names"] is False


def test_update_clears_old_attachments_and_retains_product_controls():
    control = {"type": "actions", "elements": []}
    request = message_request(
        RichReply("Complete", blocks=(control,)),
        channel_id="C123", thread_ts="123.001", message_ts="123.002",
    )
    assert request["method"] == "chat.update"
    assert request["payload"]["ts"] == "123.002"
    assert "thread_ts" not in request["payload"]
    assert request["payload"]["attachments"] == []
    assert request["payload"]["blocks"] == [control]


def test_plain_update_clears_blocks_without_adding_fallback_sections():
    request = message_request(
        RichReply("Complete"), channel_id="C123", message_ts="123.002", fallback_blocks=False,
    )
    assert request["payload"]["blocks"] == []
    assert request["payload"]["attachments"] == []


def test_card_only_reply_does_not_repeat_its_accessible_fallback():
    card = {"fallback": "View PR", "text": "Changes are ready"}
    request = message_request(
        RichReply("View PR", attachments=(card,)), channel_id="C123", fallback_blocks=False,
    )
    assert request["payload"]["blocks"] == []
    assert request["payload"]["attachments"] == [card]


def test_author_override_is_used_for_posts_and_ignored_for_updates():
    posted = message_request(
        RichReply("Continue"), channel_id="C123",
        username="Ishaan Jaffer", icon_url="https://example.com/avatar.png",
    )
    assert posted["payload"]["username"] == "Ishaan Jaffer"
    assert posted["payload"]["icon_url"] == "https://example.com/avatar.png"

    updated = message_request(
        RichReply("Continue"), channel_id="C123", message_ts="123.002",
        username="Ishaan Jaffer", icon_url="https://example.com/avatar.png",
    )
    assert "username" not in updated["payload"]
    assert "icon_url" not in updated["payload"]


@pytest.mark.parametrize("kwargs", [
    {"channel_id": "invalid"},
    {"channel_id": "C123", "thread_ts": "invalid"},
    {"channel_id": "C123", "message_ts": "invalid"},
    {"channel_id": "C123", "metadata": []},
    {"channel_id": "C123", "username": "  "},
    {"channel_id": "C123", "username": "Ishaan", "icon_url": "http://example.com/avatar.png"},
    {"channel_id": "C123", "icon_url": "https://example.com/avatar.png"},
])
def test_request_rejects_invalid_provider_inputs(kwargs):
    with pytest.raises(ValueError):
        message_request(RichReply("Ready"), **kwargs)
