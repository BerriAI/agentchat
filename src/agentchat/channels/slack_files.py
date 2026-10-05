"""Incoming Slack files for Socket Mode and host-owned webhook transports.

Events carry references only. Hosts authorize the request before explicitly
downloading, then own content inspection, persistence and model integration.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from urllib.parse import urlsplit

import aiohttp

from agentchat.channels.slack_media import DeliveryGuard, SlackRequest
from agentchat.models import Attachment, DownloadedFile

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


def _download_url(value: object) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        target = urlsplit(value)
        if (target.scheme != "https" or target.hostname != "files.slack.com"
                or target.port not in (None, 443) or target.username or target.password
                or target.fragment or not target.path.startswith("/files-pri/")):
            raise ValueError
    except ValueError:
        raise ValueError("Slack returned an unsupported file location") from None
    return value


async def download_slack_file(
    attachment: Attachment, *, bot_token: str, request: SlackRequest,
    max_bytes: int = 10 * 1024 * 1024, before_download: DeliveryGuard | None = None,
) -> DownloadedFile:
    """Resolve files.info and stream a bounded download from Slack's private host.

    ``request`` is an authenticated, single-attempt Slack API transport, called
    with ``files.info`` and a file ID. The bot token is used only for the private
    download, with redirects disabled. The optional guard runs before each
    external step and before returning bytes, allowing policy revocation.
    No request is retried and provider failures remain visible to the host.
    """
    if not isinstance(attachment.id, str) or not FILE_ID.fullmatch(attachment.id):
        raise ValueError("Expected a Slack file identifier")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("Expected a positive attachment byte limit")
    if not bot_token:
        raise ValueError("A Slack bot token is required for attachment downloads")

    async def guard() -> None:
        if before_download is not None:
            await before_download()

    limit_error = f"Slack file exceeds the {max_bytes / (1024 * 1024):g} MB download limit."
    await guard()
    info = await request("files.info", {"file": attachment.id})
    file = info.get("file")
    if info.get("ok") is not True or not isinstance(file, Mapping):
        raise ValueError("This Slack file is unavailable. Upload the attachment again.")
    if file.get("id") != attachment.id or file.get("is_external"):
        raise ValueError("This Slack file is unavailable. Upload the attachment again.")
    size = file.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("Slack returned an invalid file size")
    if size > max_bytes:
        raise ValueError(limit_error)
    url = _download_url(file.get("url_private_download") or file.get("url_private"))
    name = file.get("name")
    name = _filename(name) if isinstance(name, str) else "attachment"
    media_type = file.get("mimetype")
    await guard()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as client:
        async with client.get(url, headers={"Authorization": "Bearer " + bot_token},
                              allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError("Could not read this Slack attachment. Upload it again.")
            chunks, received = [], 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError(limit_error)
                chunks.append(chunk)
    await guard()
    raw = b"".join(chunks)
    if not raw:
        raise ValueError("This Slack file was empty. Upload the attachment again.")
    return DownloadedFile(id=attachment.id, name=name, content=raw,
                          media_type=media_type if isinstance(media_type, str) else None)
