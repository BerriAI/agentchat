"""BlueBubbles new-message events to SDK messages; no downloads or I/O."""

from __future__ import annotations

from types import MappingProxyType
from urllib.parse import quote

from agentchat.models import Attachment, Message, Sender


def normalize(event: object, *, instance_id: str) -> Message | None:
    if not isinstance(event, dict) or event.get("isFromMe") is not False:
        return None
    if (
        event.get("isSystemMessage")
        or event.get("isServiceMessage")
        or event.get("isAutoReply")
        or event.get("isSpam")
        or event.get("associatedMessageType") not in (None, 0)
        or event.get("itemType") not in (None, 0)
    ):
        return None
    guid, handle, chats = event.get("guid"), event.get("handle"), event.get("chats")
    if not isinstance(handle, dict) or not isinstance(chats, list) or len(chats) != 1:
        return None
    chat_guid = chats[0].get("guid") if isinstance(chats[0], dict) else None
    address = handle.get("address")
    if not all(isinstance(value, str) and value for value in (guid, chat_guid, address)):
        return None
    if not chat_guid.startswith("iMessage;"):
        return None
    text = event.get("text") or ""
    if not isinstance(text, str):
        return None
    attachments = []
    items = event.get("attachments")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("guid"), str) and item["guid"]:
            attachments.append(
                Attachment(
                    id=item["guid"],
                    name=item.get("transferName"),
                    media_type=item.get("mimeType"),
                    size=item.get("totalBytes"),
                )
            )
    text = text.replace("\ufffc", "").strip()
    if not text and not attachments:
        return None
    scope = quote(instance_id, safe="")
    return Message(
        id=f"imessage:{scope}:{guid}",
        conversation_id=f"imessage:{scope}:{quote(chat_guid, safe='')}",
        channel="imessage",
        sender=Sender(id=address),
        text=text,
        role="user",
        attachments=tuple(attachments),
        metadata=MappingProxyType(
            {
                "instance_id": instance_id,
                "chat_guid": chat_guid,
                "message_guid": guid,
            }
        ),
    )
