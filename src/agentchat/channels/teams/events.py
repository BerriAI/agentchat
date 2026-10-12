"""Normalize authenticated Teams activities without retaining request/turn objects."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from urllib.parse import quote

from agentchat.models import Attachment, Message, Sender


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def normalize(event: Mapping[str, object], *, app_id: str, tenant_id: str) -> Message | None:
    if event.get("type") != "message" or event.get("channelId") != "msteams":
        return None
    conversation, sender = event.get("conversation"), event.get("from")
    recipient, data = event.get("recipient"), event.get("channelData")
    if not all(isinstance(value, dict) for value in (conversation, sender, recipient, data)):
        return None
    tenant_data = data.get("tenant")
    tenant = tenant_data.get("id") if isinstance(tenant_data, dict) else None
    if tenant != tenant_id or recipient.get("id") not in (app_id, f"28:{app_id}"):
        return None
    activity_id, sender_id, conversation_id = (
        event.get("id"),
        sender.get("id"),
        conversation.get("id"),
    )
    if not all(
        isinstance(value, str) and value
        for value in (
            activity_id,
            sender_id,
            conversation_id,
            event.get("serviceUrl"),
        )
    ):
        return None
    if sender_id == recipient.get("id") or sender.get("role") == "bot":
        return None
    kind = conversation.get("conversationType")
    if kind not in ("personal", "groupChat", "channel"):
        return None
    text = event.get("text") or ""
    if not isinstance(text, str):
        return None
    mentioned = False
    for entity in _items(event.get("entities")):
        if (
            isinstance(entity, dict)
            and entity.get("type") == "mention"
            and isinstance(entity.get("mentioned"), dict)
            and entity["mentioned"].get("id") == recipient["id"]
        ):
            mentioned = True
            if isinstance(entity.get("text"), str):
                text = text.replace(entity["text"], "")
    if kind != "personal" and not mentioned:
        return None
    attachments = []
    for index, item in enumerate(_items(event.get("attachments"))):
        if not isinstance(item, dict):
            continue
        # Teams uses uniqueId for download-info files; never expose contentUrl as an ID.
        info = item.get("content")
        file_id = info.get("uniqueId") if isinstance(info, dict) else None
        attachments.append(
            Attachment(
                id=file_id if isinstance(file_id, str) and file_id else f"{activity_id}:{index}",
                name=item.get("name"),
                media_type=item.get("contentType"),
            )
        )
    if not text.strip() and not attachments:
        return None
    root = None
    if kind == "channel":
        suffix = conversation_id.partition(";messageid=")[2]
        root = event.get("replyToId") or suffix or activity_id
        if not isinstance(root, str):
            return None
    # Teams may append ;messageid=<root> to channel conversation IDs.
    base_id = conversation_id.split(";messageid=", 1)[0] if root else conversation_id
    identity = ":".join(quote(value, safe="") for value in (app_id, tenant_id, base_id))
    if root:
        identity += f":{quote(root, safe='')}"
    route = {
        "type": "message",
        "channelId": "msteams",
        "serviceUrl": event["serviceUrl"],
        "conversation": dict(conversation),
        "from": dict(sender),
        "recipient": dict(recipient),
        "channelData": {"tenant": {"id": tenant_id}},
    }
    if root:
        route["id"] = root
    return Message(
        id=f"teams:{app_id}:{tenant_id}:{quote(conversation_id, safe='')}:{activity_id}",
        conversation_id=f"teams:{identity}",
        channel="teams",
        sender=Sender(id=sender_id, display_name=sender.get("name")),
        text=text.strip(),
        role="user",
        thread_id=root,
        attachments=tuple(attachments),
        metadata=MappingProxyType(
            {
                "app_id": app_id,
                "tenant_id": tenant_id,
                "activity_id": activity_id,
                "continuation_activity": route,
            }
        ),
    )
