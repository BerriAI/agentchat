"""Pure Slack text and rich-reply payload formatting."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from agentchat.models import RichReply


def text_blocks(text: str) -> list[dict[str, object]]:
    """Expand text in one message, respecting Slack's 3,000-character sections."""
    if not text.strip():
        raise ValueError("Slack replies require readable text")
    blocks: list[dict[str, object]] = []
    opened = False
    while text:
        # Reserve space to close and reopen fenced code at section boundaries.
        limit = 2992
        end = len(text) if len(text) <= limit else text.rfind("\n", 0, limit) + 1
        if end < limit // 2 and len(text) > limit:
            end = limit
        # Never cut through a triple-backtick delimiter.
        for offset in (2, 1):
            if end >= offset and text[end - offset:end - offset + 3] == "```":
                end -= offset
                break
        part, text = text[:end], text[end:]
        in_fence = opened ^ (part.count("```") % 2 == 1)
        body = ("```\n" if opened else "") + part + ("\n```" if in_fence else "")
        blocks.append({"type": "section", "expand": True,
                       "text": {"type": "mrkdwn", "text": body, "verbatim": True}})
        opened = in_fence
        if len(blocks) > 50:
            raise ValueError("Slack replies support at most 50 sections; split the reply")
    return blocks


def _expanded_blocks(blocks: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    result = []
    for block in blocks:
        copied = dict(block)
        if copied.get("type") == "section":
            copied.setdefault("expand", True)
        result.append(copied)
    return result


def rich_payload(reply: RichReply) -> dict[str, object]:
    """Slack-compatible content only; the channel supplies routing separately."""
    if not reply.text.strip():
        raise ValueError("Rich replies require fallback text")
    payload: dict[str, object] = {
        "text": reply.text, "unfurl_links": False, "unfurl_media": False,
        "parse": "none", "link_names": False,
    }
    payload["blocks"] = _expanded_blocks(reply.blocks) if reply.blocks else text_blocks(reply.text)
    if reply.attachments:
        attachments = []
        for attachment in reply.attachments:
            copied = dict(attachment)
            if "blocks" in copied:
                copied["blocks"] = _expanded_blocks(
                    cast(Sequence[Mapping[str, object]], copied["blocks"])
                )
            attachments.append(copied)
        payload["attachments"] = attachments
    return payload
