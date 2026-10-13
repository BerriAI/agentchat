import re
import string
from typing import NamedTuple

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

_BOLD = "bold"
_ITALIC = "italic"
_CODE = "code"
_PRE = "pre"
_STRIKETHROUGH = "strikethrough"
_SPOILER = "spoiler"
_BLOCKQUOTE = "blockquote"
_TEXT_LINK = "text_link"

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})[ \t]*([^\n`]*)")
_HEADING = re.compile(r"#{1,6}\s+(.*)")
_BULLET = re.compile(r"(\s*)[-*+]\s+(.*)")
_NUMBERED = re.compile(r"(\s*)(\d+)\.\s+(.*)")
_QUOTE = re.compile(r">\s?(.*)")
# Telegram rejects the whole message when a text_link url fails its link check.
_LINK = re.compile(r'\[([^\]\n]*)\]\(((?:https?|tg)://[^)\s]+)(?:\s+"[^"]*")?\)')
_WORD_CHAR = re.compile(r"\w", re.ASCII)
_ESCAPABLE = frozenset(string.punctuation)

_INLINE = (
    ("```", _CODE),
    ("**", _BOLD),
    ("~~", _STRIKETHROUGH),
    ("||", _SPOILER),
    ("`", _CODE),
    ("*", _ITALIC),
    ("_", _ITALIC),
)
_INTRAWORD_LITERAL = frozenset({"*", "_"})


def _utf16_len(text: str) -> int:
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


class RenderedText(NamedTuple):
    text: str
    entities: list[dict]


class _TextBuilder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.entities: list[dict] = []

    def add(self, segment: str, entity_type: str | None = None, **extra: str) -> None:
        if not segment:
            return
        size = _utf16_len(segment)
        if entity_type:
            self.entities.append(
                {"type": entity_type, "offset": self.length, "length": size, **extra}
            )
        self.parts.append(segment)
        self.length += size

    def wrap(self, start: int, entity_type: str, **extra: str) -> None:
        if self.length > start:
            self.entities.append(
                {"type": entity_type, "offset": start, "length": self.length - start, **extra}
            )


def _is_word_char(src: str, index: int) -> bool:
    return 0 <= index < len(src) and bool(_WORD_CHAR.match(src[index]))


def _render_inline(
    builder: _TextBuilder, src: str, active: frozenset[str] = frozenset(),
) -> None:
    i = 0
    plain: list[str] = []

    def flush() -> None:
        if plain:
            builder.add("".join(plain))
            plain.clear()

    while i < len(src):
        if src[i] == "\\" and i + 1 < len(src) and src[i + 1] in _ESCAPABLE:
            plain.append(src[i + 1])
            i += 2
            continue

        link = _LINK.match(src, i)
        if link:
            flush()
            start = builder.length
            _render_inline(builder, link[1], active | {_TEXT_LINK})
            builder.wrap(start, _TEXT_LINK, url=link[2])
            i = link.end()
            continue

        for marker, entity_type in _INLINE:
            if not src.startswith(marker, i) or entity_type in active:
                continue
            intraword_literal = marker in _INTRAWORD_LITERAL
            if intraword_literal and _is_word_char(src, i - 1):
                continue
            start_inner = i + len(marker)
            end = src.find(marker, start_inner)
            if end in (-1, start_inner):
                continue
            if intraword_literal and _is_word_char(src, end + len(marker)):
                continue
            inner = src[start_inner:end]
            if entity_type != _CODE and (inner[0].isspace() or inner[-1].isspace()):
                continue
            flush()
            if entity_type == _CODE:
                builder.add(inner, None if _TEXT_LINK in active else _CODE)
            else:
                start = builder.length
                _render_inline(builder, inner, active | {entity_type})
                builder.wrap(start, entity_type)
            i = end + len(marker)
            break
        else:
            plain.append(src[i])
            i += 1
    flush()


def _render_line(builder: _TextBuilder, line: str) -> None:
    if heading := _HEADING.fullmatch(line):
        start = builder.length
        _render_inline(builder, heading[1])
        builder.wrap(start, _BOLD)
    elif bullet := _BULLET.fullmatch(line):
        builder.add(f"{bullet[1]}• ")
        _render_inline(builder, bullet[2])
    elif numbered := _NUMBERED.fullmatch(line):
        builder.add(f"{numbered[1]}{numbered[2]}. ")
        _render_inline(builder, numbered[3])
    else:
        _render_inline(builder, line)


def _render_block(builder: _TextBuilder, block: list[str]) -> None:
    quote_start: int | None = None
    for n, line in enumerate(block):
        quote = _QUOTE.fullmatch(line)
        if quote_start is not None and not quote:
            builder.wrap(quote_start, _BLOCKQUOTE)
            quote_start = None
        if n:
            builder.add("\n")
        if quote:
            if quote_start is None:
                quote_start = builder.length
            _render_inline(builder, quote[1])
        else:
            _render_line(builder, line)
    if quote_start is not None:
        builder.wrap(quote_start, _BLOCKQUOTE)


def render(markdown: str) -> RenderedText:
    builder = _TextBuilder()
    lines = markdown.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        if fence := _FENCE.fullmatch(lines[i]):
            closer = fence[1][0] * 3
            language = fence[2].split()[0] if fence[2].strip() else None
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(closer):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body).rstrip("\n")
            if code:
                if builder.length:
                    builder.add("\n\n")
                builder.add(code, _PRE, **({"language": language} if language else {}))
            continue

        if not lines[i].strip():
            i += 1
            continue

        block: list[str] = []
        while i < len(lines) and lines[i].strip() and not _FENCE.fullmatch(lines[i]):
            block.append(lines[i])
            i += 1
        if builder.length:
            builder.add("\n\n")
        _render_block(builder, block)

    order = sorted(
        range(len(builder.entities)),
        key=lambda n: (builder.entities[n]["offset"], -builder.entities[n]["length"], -n),
    )
    return RenderedText("".join(builder.parts), [builder.entities[n] for n in order])


def text_payload(content: str) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Telegram replies require nonempty text")
    text, entities = render(content)
    if not text.strip():
        raise ValueError("Telegram replies require nonempty text")
    if _utf16_len(text) > TELEGRAM_TEXT_LIMIT:
        raise ValueError("Telegram replies must fit 4,096 characters; send separate replies")
    payload: dict = {"text": text}
    if entities:
        payload["entities"] = entities
    payload["link_preview_options"] = {"is_disabled": True}
    return payload


def truncate_caption(title: str) -> str:
    units = title.encode("utf-16-le")[: TELEGRAM_CAPTION_LIMIT * 2]
    return units.decode("utf-16-le", errors="ignore")
