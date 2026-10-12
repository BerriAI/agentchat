"""Slack file transfers for Socket Mode and host-owned webhook transports.

Events carry references only. Hosts authorize explicit downloads and supply
bytes for uploads; they own inspection, persistence and durable delivery.
Transfers are never retried here.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import aiohttp

from agentchat.channels.slack.events import FILE_ID, _filename
from agentchat.models import Attachment, DownloadedFile, UploadedFile, UploadFile

SlackRequest = Callable[[str, dict[str, object]], Awaitable[Mapping[str, object]]]
DeliveryGuard = Callable[[], Awaitable[None]]


def validate_destination(channel_id: str, thread_ts: str | None) -> None:
    if not isinstance(channel_id, str) or not re.fullmatch(r"[CDG][A-Z0-9]+", channel_id):
        raise ValueError("Expected a Slack channel destination")
    if thread_ts is not None and (
        not isinstance(thread_ts, str) or not re.fullmatch(r"\d+\.\d+", thread_ts)
    ):
        raise ValueError("Expected a Slack thread destination")


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
