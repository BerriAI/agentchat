"""Pure normalization of Slack events and attachment references."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from types import MappingProxyType

from agentchat.models import Attachment, Message, Sender

FILE_ID = re.compile(r"F[A-Z0-9]{1,30}")


def slack_attachments(files: object, *, limit: int = 5) -> tuple[Attachment, ...]:
    """Normalize a bounded file list without retaining URLs, tokens or file bytes."""
    if not 1 <= limit <= 100:
        raise ValueError("Attachment limit must be between 1 and 100")
    if not isinstance(files, list):
        return ()
    selected: dict[str, Attachment] = {}
    for item in files[:limit]:
        if not isinstance(item, Mapping):
            continue
        file_id = item.get("id")
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            continue
        name, media_type, size = item.get("name"), item.get("mimetype"), item.get("size")
        selected.setdefault(file_id, Attachment(
            id=file_id, name=_filename(name) if isinstance(name, str) else None,
            media_type=media_type[:255] if isinstance(media_type, str) else None,
            size=size if isinstance(size, int) and not isinstance(size, bool)
            and size >= 0 else None,
        ))
    return tuple(selected.values())


def _filename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    return name.strip(" .")[:180] or "attachment"


def to_message(
    payload: Mapping[str, object],
    event: Mapping[str, object],
    *,
    bot_user_id: str | None,
    workspace_id: str | None,
    channel_name: str = "slack",
) -> Message | None:
    """Normalize an incoming event without fetching files or performing other I/O."""
    event_type = event.get("type")
    is_direct_message = event_type == "message" and event.get("channel_type") == "im"
    if (event.get("bot_id") is not None or event.get("bot_profile") is not None
            or event.get("subtype") not in (None, "file_share")):
        return None
    user_id = event.get("user")
    channel_id = event.get("channel")
    timestamp = event.get("ts")
    text = event.get("text", "")
    attachments = slack_attachments(event.get("files"))
    if not isinstance(user_id, str) or not user_id or user_id == bot_user_id:
        return None
    if not isinstance(channel_id, str) or not channel_id:
        return None
    if not isinstance(timestamp, str) or not timestamp:
        return None
    if not isinstance(text, str) or (not text.strip() and not attachments):
        return None
    team_id = payload.get("team_id")
    if not isinstance(team_id, str) or not team_id:
        return None
    if workspace_id is not None and team_id != workspace_id:
        return None
    thread_timestamp_value = event.get("thread_ts")
    if thread_timestamp_value is not None and (
        not isinstance(thread_timestamp_value, str) or not thread_timestamp_value
    ):
        return None
    thread_timestamp = (
        thread_timestamp_value if isinstance(thread_timestamp_value, str) else timestamp
    )
    bot_mention = f"<@{bot_user_id}>" if bot_user_id else None
    is_channel_message = event_type == "message" and event.get("channel_type") in (
        "channel",
        "group",
    )
    is_mention = event_type == "app_mention" or (
        is_channel_message and bot_mention is not None and bot_mention in text
    )
    is_thread_reply = is_channel_message and thread_timestamp_value is not None
    if not (is_direct_message or is_mention or is_thread_reply):
        return None
    conversation_id = (
        f"slack:{team_id}:{channel_id}"
        if is_direct_message
        else f"slack:{team_id}:{channel_id}:{thread_timestamp}"
    )
    event_id_value = payload.get("event_id")
    event_id = (
        event_id_value if isinstance(event_id_value, str) else f"{channel_id}:{timestamp}"
    )
    normalized_text = text.replace(bot_mention, "").strip() if bot_mention else text.strip()
    if not normalized_text and not attachments:
        return None
    return Message(
        id=f"slack:{team_id}:{channel_id}:{timestamp}",
        conversation_id=conversation_id,
        channel=channel_name,
        sender=Sender(id=user_id),
        text=normalized_text,
        role="user",
        thread_id=thread_timestamp_value if is_direct_message else thread_timestamp,
        addressed=is_direct_message or is_mention,
        attachments=attachments,
        metadata=MappingProxyType(
            {
                "channel_id": channel_id,
                "team_id": team_id,
                "event_id": event_id,
                "channel_type": "im"
                if is_direct_message
                else event.get("channel_type", "channel"),
                "reply_thread_timestamp": thread_timestamp_value
                if is_direct_message
                else thread_timestamp,
                "message_timestamp": timestamp,
                "requires_subscription": not is_direct_message and not is_mention,
            }
        ),
    )
