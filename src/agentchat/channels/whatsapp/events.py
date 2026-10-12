"""Normalize signed WhatsApp Cloud API webhook batches."""

from __future__ import annotations

from types import MappingProxyType

from agentchat.models import Attachment, Message, Sender


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def normalize(payload: object, *, phone_number_id: str) -> tuple[Message, ...]:
    payload = _mapping(payload)
    if payload.get("object") != "whatsapp_business_account":
        return ()
    messages = []
    for entry in _items(payload.get("entry")):
        for change in _items(_mapping(entry).get("changes")):
            change = _mapping(change)
            value = _mapping(change.get("value"))
            if (
                change.get("field") != "messages"
                or value.get("messaging_product") != "whatsapp"
                or _mapping(value.get("metadata")).get("phone_number_id") != phone_number_id
            ):
                continue
            names = {
                item.get("wa_id"): _mapping(item.get("profile")).get("name")
                for item in _items(value.get("contacts"))
                if isinstance(item, dict) and isinstance(item.get("wa_id"), str)
            }
            for raw in _items(value.get("messages")):
                event = _mapping(raw)
                sender, message_id = event.get("from"), event.get("id")
                if (
                    not isinstance(sender, str)
                    or not sender.isascii()
                    or not sender.isdigit()
                    or not isinstance(message_id, str)
                    or not message_id
                ):
                    continue
                kind = event.get("type")
                text, attachments = "", ()
                if kind == "text":
                    text = _mapping(event.get("text")).get("body", "")
                elif kind in ("image", "audio", "video", "document", "sticker"):
                    media = _mapping(event.get(kind))
                    if isinstance(media.get("id"), str) and media["id"]:
                        attachments = (
                            Attachment(
                                id=media["id"],
                                name=media.get("filename"),
                                media_type=media.get("mime_type"),
                            ),
                        )
                        text = media.get("caption", "")
                elif kind == "button":
                    text = _mapping(event.get("button")).get("text", "")
                elif kind == "interactive":
                    interactive = _mapping(event.get("interactive"))
                    reply = interactive.get("button_reply") or interactive.get("list_reply")
                    text = _mapping(reply).get("title", "")
                if not isinstance(text, str) or (not text.strip() and not attachments):
                    continue
                messages.append(
                    Message(
                        id=f"whatsapp:{phone_number_id}:{message_id}",
                        conversation_id=f"whatsapp:{phone_number_id}:{sender}",
                        channel="whatsapp",
                        sender=Sender(id=sender, display_name=names.get(sender)),
                        text=text,
                        role="user",
                        attachments=attachments,
                        metadata=MappingProxyType(
                            {
                                "phone_number_id": phone_number_id,
                                "message_id": message_id,
                                "wa_id": sender,
                                "message_type": kind,
                            }
                        ),
                    )
                )
    return tuple(messages)
