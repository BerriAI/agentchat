"""Native Telegram event normalization."""
from agentchat._native import telegram_normalize as normalize

__all__ = ["normalize"]

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
FILE_KINDS = ("animation", "document", "audio", "video", "voice", "video_note", "sticker")
