"""The native Slack crate consumes these exact Python-reference fixtures too."""
import json
from pathlib import Path

import pytest

from agentchat.channels.slack.events import to_message

FIXTURES = json.loads(
    (Path(__file__).parents[3] / "conformance" / "slack-events.json").read_text()
)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture["name"])
def test_normalization_matches_shared_contract(fixture):
    message = to_message(
        fixture["payload"], fixture["payload"]["event"],
        bot_user_id=fixture["bot_user_id"], workspace_id=fixture["workspace_id"],
    )
    if fixture["expected"] is None:
        assert message is None
        return
    assert message is not None
    actual = {
        "id": message.id, "conversation_id": message.conversation_id, "channel": message.channel,
        "sender": {"id": message.sender.id, "display_name": message.sender.display_name},
        "text": message.text, "role": message.role, "metadata": dict(message.metadata),
        "thread_id": message.thread_id, "addressed": message.addressed,
        "attachments": [
            {"id": a.id, "name": a.name, "media_type": a.media_type, "size": a.size}
            for a in message.attachments
        ],
    }
    assert actual == fixture["expected"]
