"""Safe Slack rendering shared by Socket Mode and application-owned webhooks."""
from __future__ import annotations

import html
from collections.abc import Mapping

from agentchat.models import Message, Sender

EVENT_TYPE = "agentchat_user_mirror_v1"


def mirror_payload(message: Message, *, origin: str = "web") -> dict[str, object]:
    """Build content only; the transport must supply a trusted Slack destination.

    Split long inputs into stable chunks before calling. No Slack user is
    impersonated: the bot posts plain text with the original sender's label.
    """
    if message.role != "user" or not message.id or not message.sender.id:
        raise ValueError("A mirror requires a user input with stable message and sender IDs")
    if not message.text or len(message.text) > 2800:
        raise ValueError("Mirror text must contain 1-2800 characters; split long inputs first")
    if not origin.strip() or len(origin) > 64:
        raise ValueError("Mirror origin must contain 1-64 characters")
    if len(message.id) > 256 or len(message.sender.id) > 256:
        raise ValueError("Mirror IDs must be at most 256 characters")
    name = " ".join((message.sender.display_name or message.sender.id).split())[:160]
    origin = " ".join(origin.split())
    label = f"{name} · via {origin}"
    return {
        "text": html.escape(f"{label}\n{message.text}", quote=False),
        "blocks": [
            {"type": "context", "elements": [
                {"type": "plain_text", "text": label, "emoji": False},
            ]},
            {"type": "section", "block_id": "agentchat_mirror_body", "text": {
                "type": "plain_text", "text": message.text, "emoji": False,
            }},
        ],
        "metadata": {"event_type": EVENT_TYPE, "event_payload": {
            "message_id": message.id, "sender_id": message.sender.id,
            "sender_name": name, "origin": origin,
        }},
        "mrkdwn": False, "parse": "none", "link_names": False,
        "unfurl_links": False, "unfurl_media": False,
    }


def read_mirror(
    record: Mapping[str, object], *, bot_user_id: str | None
) -> tuple[Sender, str] | None:
    """Read our own bot's typed mirror, never another sender's claimed identity."""
    if not bot_user_id or record.get("user") != bot_user_id:
        return None
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("event_type") != EVENT_TYPE:
        return None
    payload, blocks = metadata.get("event_payload"), record.get("blocks")
    if not isinstance(payload, Mapping) or not isinstance(blocks, list):
        return None
    if not all(isinstance(payload.get(k), str) and payload[k]
               for k in ("message_id", "sender_id", "sender_name", "origin")):
        return None
    for block in blocks:
        if not isinstance(block, Mapping) or block.get("block_id") != "agentchat_mirror_body":
            continue
        text = block.get("text")
        if (isinstance(text, Mapping) and text.get("type") == "plain_text"
                and isinstance(text.get("text"), str)):
            sender = Sender(id=payload["sender_id"], display_name=payload["sender_name"])
            return sender, text["text"]
    return None
