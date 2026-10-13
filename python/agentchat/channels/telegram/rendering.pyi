# Public compatibility signatures; update with the corresponding Rust binding API.
from typing import NamedTuple

TELEGRAM_TEXT_LIMIT: int
TELEGRAM_CAPTION_LIMIT: int

class RenderedText(NamedTuple):
    text: str
    entities: list[dict]

def render(markdown: str) -> RenderedText: ...
def text_payload(content: str) -> dict: ...
def truncate_caption(title: str) -> str: ...
