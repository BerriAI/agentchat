"""Native Telegram Markdown rendering."""
from typing import NamedTuple

from agentchat._native import (
    telegram_render as render,
)
from agentchat._native import (
    telegram_text_payload as text_payload,
)
from agentchat._native import (
    telegram_truncate_caption as truncate_caption,
)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


class RenderedText(NamedTuple):
    text: str
    entities: list[dict]


__all__ = ["RenderedText", "render", "text_payload", "truncate_caption"]
