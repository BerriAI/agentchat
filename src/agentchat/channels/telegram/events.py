from __future__ import annotations

from types import MappingProxyType
from typing import NamedTuple

from agentchat.models import Attachment, Message, Sender

TELEGRAM_CHANNEL = "telegram"
UPDATE_MESSAGE = "message"
UPDATE_CALLBACK_QUERY = "callback_query"
METADATA_BOT_ID = "bot_id"
METADATA_CHAT_ID = "chat_id"
METADATA_CHAT_TYPE = "chat_type"
METADATA_MESSAGE_ID = "message_id"
METADATA_THREAD_ID = "message_thread_id"
METADATA_CALLBACK_QUERY_ID = "callback_query_id"
METADATA_CALLBACK_DATA = "callback_data"
CHAT_PRIVATE = "private"
GROUP_CHATS = frozenset({"group", "supergroup"})
ENTITY_MENTION = "mention"
ENTITY_COMMAND = "bot_command"
ENTITY_TEXT_MENTION = "text_mention"
PHOTO_MEDIA_TYPE = "image/jpeg"
# Animations also arrive as a document, so the earlier kind wins for a shared file_id.
FILE_KINDS = ("animation", "document", "audio", "video", "voice", "video_note", "sticker")


class _Span(NamedTuple):
    start: int
    end: int
    text: str


class _BotAddress(NamedTuple):
    is_addressed: bool
    text: str


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sender(user: dict) -> Sender | None:
    if not _is_non_bool_int(user.get("id")) or user.get("is_bot") is not False:
        return None
    names = (user.get("first_name"), user.get("last_name"))
    display_name = " ".join(name for name in names if isinstance(name, str) and name)
    return Sender(id=str(user["id"]), display_name=display_name or None)


def _entity_span(units: bytes, entity: dict) -> _Span | None:
    offset, length = entity.get("offset"), entity.get("length")
    if not _is_non_bool_int(offset) or not _is_non_bool_int(length) or offset < 0 or length <= 0:
        return None
    end = offset + length
    if end * 2 > len(units):
        return None
    try:
        return _Span(offset, end, units[offset * 2:end * 2].decode("utf-16-le"))
    except UnicodeDecodeError:
        return None


def _strip_bot_address(
    *, text: str, entities: list, bot_id: int, bot_username: str,
) -> _BotAddress:
    units = text.encode("utf-16-le")
    mention = "@" + bot_username.lower()
    is_addressed, removals = False, []
    for raw in entities:
        entity = _as_dict(raw)
        span = _entity_span(units, entity)
        if span is None:
            continue
        start, end, value = span
        kind = entity.get("type")
        if kind == ENTITY_MENTION and value.lower() == mention:
            is_addressed = True
            removals.append((start, end))
        elif kind == ENTITY_COMMAND and value.lower().endswith(mention):
            is_addressed = True
            removals.append((end - len(mention), end))
        elif kind == ENTITY_TEXT_MENTION and _as_dict(entity.get("user")).get("id") == bot_id:
            is_addressed = True
    kept, cursor = [], 0
    for start, end in sorted(removals):
        if start < cursor:
            continue
        kept.append(units[cursor * 2:start * 2])
        cursor = end
    kept.append(units[cursor * 2:])
    return _BotAddress(is_addressed, b"".join(kept).decode("utf-16-le").strip())


def _attachments(message: dict) -> tuple[Attachment, ...]:
    found: dict[str, Attachment] = {}
    photos = [item for item in _as_list(message.get("photo")) if isinstance(item, dict)]
    if photos and isinstance(photos[-1].get("file_id"), str) and photos[-1]["file_id"]:
        size = photos[-1].get("file_size")
        found[photos[-1]["file_id"]] = Attachment(
            id=photos[-1]["file_id"],
            media_type=PHOTO_MEDIA_TYPE,
            size=size if _is_non_bool_int(size) else None,
        )
    for kind in FILE_KINDS:
        media = _as_dict(message.get(kind))
        file_id = media.get("file_id")
        if not isinstance(file_id, str) or not file_id or file_id in found:
            continue
        name, media_type = media.get("file_name"), media.get("mime_type")
        size = media.get("file_size")
        found[file_id] = Attachment(
            id=file_id,
            name=name if isinstance(name, str) and name else None,
            media_type=media_type if isinstance(media_type, str) and media_type else None,
            size=size if _is_non_bool_int(size) else None,
        )
    return tuple(found.values())


def _button_label(message: dict, data: str) -> str:
    keyboard = _as_dict(message.get("reply_markup")).get("inline_keyboard")
    for row in _as_list(keyboard):
        for button in _as_list(row):
            button = _as_dict(button)
            if button.get("callback_data") == data and isinstance(button.get("text"), str):
                return button["text"]
    return data


def _build_message(
    *, bot_id: int, local_id: str, chat: dict, message: dict, sender: Sender, text: str,
    attachments: tuple[Attachment, ...] = (), extra_metadata: dict | None = None,
) -> Message:
    thread = message.get("message_thread_id")
    is_forum_topic = message.get("is_topic_message") is True and _is_non_bool_int(thread)
    forum_topic = thread if is_forum_topic else None
    conversation_id = f"{TELEGRAM_CHANNEL}:{bot_id}:{chat['id']}"
    return Message(
        id=f"{TELEGRAM_CHANNEL}:{bot_id}:{local_id}",
        conversation_id=(
            conversation_id if forum_topic is None else f"{conversation_id}:{forum_topic}"
        ),
        channel=TELEGRAM_CHANNEL,
        sender=sender,
        text=text,
        role="user",
        thread_id=None if forum_topic is None else str(forum_topic),
        attachments=attachments,
        metadata=MappingProxyType(
            {
                METADATA_BOT_ID: bot_id,
                METADATA_CHAT_ID: chat["id"],
                METADATA_CHAT_TYPE: chat["type"],
                METADATA_MESSAGE_ID: message["message_id"],
                METADATA_THREAD_ID: forum_topic,
                **(extra_metadata or {}),
            }
        ),
    )


def _supported_chat(message: dict) -> dict | None:
    chat = _as_dict(message.get("chat"))
    message_id = message.get("message_id")
    if (
        not _is_non_bool_int(chat.get("id"))
        or (chat.get("type") != CHAT_PRIVATE and chat.get("type") not in GROUP_CHATS)
        or not _is_non_bool_int(message_id)
        or message_id <= 0
    ):
        return None
    return chat


def _normalize_callback(callback: dict, *, bot_id: int) -> Message | None:
    message = _as_dict(callback.get("message"))
    data, callback_id = callback.get("data"), callback.get("id")
    sender = _sender(_as_dict(callback.get("from")))
    chat = _supported_chat(message)
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
    return _build_message(
        bot_id=bot_id,
        local_id=f"callback:{callback_id}",
        chat=chat,
        message=message,
        sender=sender,
        text=_button_label(message, data),
        extra_metadata={METADATA_CALLBACK_QUERY_ID: callback_id, METADATA_CALLBACK_DATA: data},
    )


def normalize(update: object, *, bot_id: int, bot_username: str) -> Message | None:
    update = _as_dict(update)
    if UPDATE_CALLBACK_QUERY in update:
        return _normalize_callback(_as_dict(update[UPDATE_CALLBACK_QUERY]), bot_id=bot_id)
    message = _as_dict(update.get(UPDATE_MESSAGE))
    sender = _sender(_as_dict(message.get("from")))
    chat = _supported_chat(message)
    if sender is None or chat is None:
        return None
    if isinstance(message.get("text"), str):
        text, entities = message["text"], _as_list(message.get("entities"))
    elif isinstance(message.get("caption"), str):
        text, entities = message["caption"], _as_list(message.get("caption_entities"))
    else:
        text, entities = "", []
    is_addressed, text = _strip_bot_address(
        text=text, entities=entities, bot_id=bot_id, bot_username=bot_username,
    )
    reply_to = _as_dict(_as_dict(message.get("reply_to_message")).get("from"))
    if chat["type"] != CHAT_PRIVATE and not is_addressed and reply_to.get("id") != bot_id:
        return None
    attachments = _attachments(message)
    if not text and not attachments:
        return None
    return _build_message(
        bot_id=bot_id,
        local_id=f"{chat['id']}:{message['message_id']}",
        chat=chat,
        message=message,
        sender=sender,
        text=text,
        attachments=attachments,
    )
