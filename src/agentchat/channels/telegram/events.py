"""Normalize Telegram Bot API updates; no network calls."""

from __future__ import annotations

from types import MappingProxyType

from agentchat.models import Attachment, Message, Sender

CHAT_PRIVATE = "private"
GROUP_CHATS = frozenset({"group", "supergroup"})
ENTITY_MENTION = "mention"
ENTITY_COMMAND = "bot_command"
ENTITY_TEXT_MENTION = "text_mention"
PHOTO_MEDIA_TYPE = "image/jpeg"
# Animations also arrive as a document, so the earlier kind wins for a shared file_id.
FILE_KINDS = ("animation", "document", "audio", "video", "voice", "video_note", "sticker")


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def _is_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sender(user: dict) -> Sender | None:
    if not _is_id(user.get("id")) or user.get("is_bot") is not False:
        return None
    names = (user.get("first_name"), user.get("last_name"))
    display_name = " ".join(name for name in names if isinstance(name, str) and name)
    return Sender(id=str(user["id"]), display_name=display_name or None)


def _span(units: bytes, entity: dict) -> tuple[int, int, str] | None:
    offset, length = entity.get("offset"), entity.get("length")
    if not _is_id(offset) or not _is_id(length) or offset < 0 or length <= 0:
        return None
    end = offset + length
    if end * 2 > len(units):
        return None
    try:
        return offset, end, units[offset * 2:end * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def _address(
    *, text: str, entities: list, bot_id: int, bot_username: str,
) -> tuple[bool, str]:
    units = text.encode("utf-16-le")
    mention = "@" + bot_username.lower()
    addressed, removals = False, []
    for raw in entities:
        entity = _mapping(raw)
        span = _span(units, entity)
        if span is None:
            continue
        start, end, value = span
        kind = entity.get("type")
        if kind == ENTITY_MENTION and value.lower() == mention:
            addressed = True
            removals.append((start, end))
        elif kind == ENTITY_COMMAND and value.lower().endswith(mention):
            addressed = True
            removals.append((end - len(mention), end))
        elif kind == ENTITY_TEXT_MENTION and _mapping(entity.get("user")).get("id") == bot_id:
            addressed = True
    kept, cursor = [], 0
    for start, end in sorted(removals):
        if start < cursor:
            continue
        kept.append(units[cursor * 2:start * 2])
        cursor = end
    kept.append(units[cursor * 2:])
    return addressed, b"".join(kept).decode("utf-16-le").strip()


def _attachments(message: dict) -> tuple[Attachment, ...]:
    found: dict[str, Attachment] = {}
    photos = [item for item in _items(message.get("photo")) if isinstance(item, dict)]
    if photos and isinstance(photos[-1].get("file_id"), str) and photos[-1]["file_id"]:
        size = photos[-1].get("file_size")
        found[photos[-1]["file_id"]] = Attachment(
            id=photos[-1]["file_id"],
            media_type=PHOTO_MEDIA_TYPE,
            size=size if _is_id(size) else None,
        )
    for kind in FILE_KINDS:
        media = _mapping(message.get(kind))
        file_id = media.get("file_id")
        if not isinstance(file_id, str) or not file_id or file_id in found:
            continue
        name, media_type = media.get("file_name"), media.get("mime_type")
        size = media.get("file_size")
        found[file_id] = Attachment(
            id=file_id,
            name=name if isinstance(name, str) and name else None,
            media_type=media_type if isinstance(media_type, str) and media_type else None,
            size=size if _is_id(size) else None,
        )
    return tuple(found.values())


def _button_label(message: dict, data: str) -> str:
    keyboard = _mapping(message.get("reply_markup")).get("inline_keyboard")
    for row in _items(keyboard):
        for button in _items(row):
            button = _mapping(button)
            if button.get("callback_data") == data and isinstance(button.get("text"), str):
                return button["text"]
    return data


def _message(
    *, bot_id: int, local_id: str, chat: dict, message: dict, sender: Sender, text: str,
    attachments: tuple[Attachment, ...] = (), extra: dict | None = None,
) -> Message:
    thread = message.get("message_thread_id")
    forum_topic = thread if message.get("is_topic_message") is True and _is_id(thread) else None
    conversation_id = f"telegram:{bot_id}:{chat['id']}"
    return Message(
        id=f"telegram:{bot_id}:{local_id}",
        conversation_id=(
            conversation_id if forum_topic is None else f"{conversation_id}:{forum_topic}"
        ),
        channel="telegram",
        sender=sender,
        text=text,
        role="user",
        thread_id=None if forum_topic is None else str(forum_topic),
        attachments=attachments,
        metadata=MappingProxyType(
            {
                "bot_id": bot_id,
                "chat_id": chat["id"],
                "chat_type": chat["type"],
                "message_id": message["message_id"],
                "message_thread_id": forum_topic,
                **(extra or {}),
            }
        ),
    )


def _chat(message: dict) -> dict | None:
    chat = _mapping(message.get("chat"))
    message_id = message.get("message_id")
    if (
        not _is_id(chat.get("id"))
        or (chat.get("type") != CHAT_PRIVATE and chat.get("type") not in GROUP_CHATS)
        or not _is_id(message_id)
        or message_id <= 0
    ):
        return None
    return chat


def _normalize_callback(callback: dict, *, bot_id: int) -> Message | None:
    message = _mapping(callback.get("message"))
    data, callback_id = callback.get("data"), callback.get("id")
    sender = _sender(_mapping(callback.get("from")))
    chat = _chat(message)
    if (
        not isinstance(data, str)
        or not data
        or not isinstance(callback_id, str)
        or not callback_id
        or sender is None
        or chat is None
        or message.get("date") == 0
    ):
        return None
    return _message(
        bot_id=bot_id,
        local_id=f"callback:{callback_id}",
        chat=chat,
        message=message,
        sender=sender,
        text=_button_label(message, data),
        extra={"callback_query_id": callback_id, "callback_data": data},
    )


def normalize(update: object, *, bot_id: int, bot_username: str) -> Message | None:
    update = _mapping(update)
    if "callback_query" in update:
        return _normalize_callback(_mapping(update["callback_query"]), bot_id=bot_id)
    message = _mapping(update.get("message"))
    sender = _sender(_mapping(message.get("from")))
    chat = _chat(message)
    if sender is None or chat is None:
        return None
    if isinstance(message.get("text"), str):
        text, entities = message["text"], _items(message.get("entities"))
    elif isinstance(message.get("caption"), str):
        text, entities = message["caption"], _items(message.get("caption_entities"))
    else:
        text, entities = "", []
    addressed, text = _address(
        text=text, entities=entities, bot_id=bot_id, bot_username=bot_username,
    )
    reply_to = _mapping(_mapping(message.get("reply_to_message")).get("from"))
    if chat["type"] != CHAT_PRIVATE and not addressed and reply_to.get("id") != bot_id:
        return None
    attachments = _attachments(message)
    if not text and not attachments:
        return None
    return _message(
        bot_id=bot_id,
        local_id=f"{chat['id']}:{message['message_id']}",
        chat=chat,
        message=message,
        sender=sender,
        text=text,
        attachments=attachments,
    )
