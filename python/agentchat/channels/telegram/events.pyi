# Public compatibility signatures; update with the corresponding Rust binding API.
from agentchat.models import Message

TELEGRAM_CHANNEL: str
UPDATE_MESSAGE: str
UPDATE_CALLBACK_QUERY: str
METADATA_BOT_ID: str
METADATA_CHAT_ID: str
METADATA_CHAT_TYPE: str
METADATA_MESSAGE_ID: str
METADATA_THREAD_ID: str
METADATA_CALLBACK_QUERY_ID: str
METADATA_CALLBACK_DATA: str
CHAT_PRIVATE: str
GROUP_CHATS: frozenset[str]
ENTITY_MENTION: str
ENTITY_COMMAND: str
ENTITY_TEXT_MENTION: str
PHOTO_MEDIA_TYPE: str
FILE_KINDS: tuple[str, ...]

def normalize(update: object, *, bot_id: int, bot_username: str) -> Message | None: ...
