"""Reusable Slack delivery for Socket Mode and host-owned webhook transports.

The caller supplies authorized bytes and a bound destination, and owns size
budgets and durable delivery. A failed invocation is never retried here.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import cast
from urllib.parse import urlsplit

import aiohttp

from agentchat.models import RichReply, UploadedFile, UploadFile

SlackRequest = Callable[[str, dict[str, object]], Awaitable[Mapping[str, object]]]
DeliveryGuard = Callable[[], Awaitable[None]]


def validate_destination(channel_id: str, thread_ts: str | None) -> None:
    if not isinstance(channel_id, str) or not re.fullmatch(r"[CDG][A-Z0-9]+", channel_id):
        raise ValueError("Expected a Slack channel destination")
    if thread_ts is not None and (
        not isinstance(thread_ts, str) or not re.fullmatch(r"\d+\.\d+", thread_ts)
    ):
        raise ValueError("Expected a Slack thread destination")


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


async def upload_bytes(url: str, raw: bytes) -> None:
    """Send bytes to Slack's signed upload URL without OAuth or redirects."""
    target = urlsplit(url)
    if (target.scheme != "https" or target.hostname != "files.slack.com"
            or target.port not in (None, 443) or target.username or target.password
            or target.fragment or not target.path.startswith("/upload/")):
        raise ValueError("Slack returned an unsupported upload destination")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
        async with client.post(url, data=raw, headers={"Content-Type": "application/octet-stream"},
                               allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError("Slack did not confirm the capture upload")


async def upload_slack_files(
    files: Sequence[UploadFile], *, request: SlackRequest, channel_id: str,
    thread_ts: str | None = None, before_send: DeliveryGuard | None = None,
) -> tuple[UploadedFile, ...]:
    """Allocate and upload a batch, then share it once in the bound conversation.

    ``request`` is a single-attempt Slack API transport. ``before_send`` may
    raise after a host's policy changes; it runs before each external step.
    An error after finalization starts can mean delivery occurred. The host
    must retain that uncertainty instead of automatically retrying this call.
    """
    validate_destination(channel_id, thread_ts)
    batch = tuple(files)
    if not batch:
        raise ValueError("Expected at least one file")
    for file in batch:
        if (not isinstance(file.filename, str) or not file.filename
                or len(file.filename.encode()) > 255 or file.filename in {".", ".."}
                or any(char in file.filename for char in "/\\")
                or any(ord(char) < 32 for char in file.filename)):
            raise ValueError("Expected a plain filename")
        if not isinstance(file.content, bytes) or not file.content:
            raise ValueError("Expected nonempty file bytes")
        if file.title is not None and not isinstance(file.title, str):
            raise ValueError("Expected a file title")

    async def guard() -> None:
        if before_send is not None:
            await before_send()

    pending: list[dict[str, str]] = []
    for file in batch:
        await guard()
        result = await request("files.getUploadURLExternal", {
            "filename": file.filename, "length": len(file.content),
        })
        file_id, url = result.get("file_id"), result.get("upload_url")
        if (result.get("ok") is not True or not isinstance(file_id, str) or not file_id
                or not isinstance(url, str) or not url
                or any(item["id"] == file_id for item in pending)):
            raise RuntimeError("Slack did not confirm an upload allocation")
        await guard()
        await upload_bytes(url, file.content)
        pending.append({"id": file_id, "title": file.title or file.filename})
    await guard()
    payload: dict[str, object] = {"files": pending, "channel_id": channel_id}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    result = await request("files.completeUploadExternal", payload)
    completed = result.get("files")
    if (result.get("ok") is not True or not isinstance(completed, list)
            or len(completed) != len(pending)
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   for item in completed)
            or {item["id"] for item in completed} != {item["id"] for item in pending}):
        raise RuntimeError("Slack media delivery could not be confirmed")
    by_id = {item["id"]: item for item in completed}
    return tuple(UploadedFile(id=item["id"], permalink=(
        by_id[item["id"]]["permalink"] if isinstance(by_id[item["id"]].get("permalink"), str)
        else None)) for item in pending)
