import asyncio
from copy import deepcopy

import pytest

from agentchat.channels import slack_media
from agentchat.models import RichReply, UploadedFile, UploadFile


def files():
    return (
        UploadFile(filename="result.png", content=b"\x89PNG\r\n", title="Result"),
        UploadFile(filename="demo.webm", content=b"\x1a\x45\xdf\xa3\x00", title="Demo"),
    )


class UploadTransport:
    def __init__(self):
        self.effects = []
        self.allocated = []
        self.complete_response = None
        self.failure_at = None

    async def request(self, api, payload):
        self.effects.append((api, payload))
        if self.failure_at == api:
            raise TimeoutError("Provider accepted request but response was lost")
        if api == "files.getUploadURLExternal":
            file_id = f"F{len(self.allocated) + 1}"
            self.allocated.append(file_id)
            return {
                "ok": True,
                "file_id": file_id,
                "upload_url": f"https://files.slack.com/upload/{file_id}?signature=example",
            }
        assert api == "files.completeUploadExternal"
        if self.complete_response is not None:
            return self.complete_response
        return {
            "ok": True,
            "files": [
                {"id": file_id, "permalink": f"https://example.slack.com/files/{file_id}"}
                for file_id in self.allocated
            ],
        }

    async def upload(self, url, raw):
        self.effects.append(("bytes", {"url": url, "raw": raw}))
        if self.failure_at == "bytes":
            raise TimeoutError("Upload response was lost")


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [None, "1700000000.000001"])
async def test_uploads_exact_bytes_then_shares_one_batch_in_destination(monkeypatch, thread):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    guards = []

    async def authorize():
        guards.append(len(transport.effects))

    result = await slack_media.upload_slack_files(
        files(), request=transport.request, channel_id="C123", thread_ts=thread,
        before_send=authorize,
    )

    assert [effect[0] for effect in transport.effects] == [
        "files.getUploadURLExternal", "bytes",
        "files.getUploadURLExternal", "bytes", "files.completeUploadExternal",
    ]
    assert guards == [0, 1, 2, 3, 4]
    for index, file in enumerate(files()):
        allocation = transport.effects[index * 2][1]
        assert allocation == {"filename": file.filename, "length": len(file.content)}
        assert transport.effects[index * 2 + 1][1]["raw"] == file.content
    expected = {
        "channel_id": "C123",
        "files": [{"id": "F1", "title": "Result"}, {"id": "F2", "title": "Demo"}],
    }
    if thread is not None:
        expected["thread_ts"] = thread
    assert transport.effects[-1][1] == expected
    assert result == (
        UploadedFile(id="F1", permalink="https://example.slack.com/files/F1"),
        UploadedFile(id="F2", permalink="https://example.slack.com/files/F2"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", range(5))
async def test_revoked_authorization_stops_before_each_external_boundary(monkeypatch, boundary):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    guard_calls = 0
    refusal = PermissionError("Session sharing was revoked")

    async def authorize():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == boundary + 1:
            raise refusal

    with pytest.raises(PermissionError) as error:
        await slack_media.upload_slack_files(
            files(), request=transport.request, channel_id="C123", before_send=authorize,
        )

    assert error.value is refusal
    assert guard_calls == boundary + 1
    assert len(transport.effects) == boundary
    assert not any(api == "files.completeUploadExternal" for api, _ in transport.effects)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,count", [
    ("files.getUploadURLExternal", 1), ("bytes", 2), ("files.completeUploadExternal", 3),
])
async def test_ambiguous_failure_propagates_without_replaying_any_stage(monkeypatch, stage, count):
    transport = UploadTransport()
    transport.failure_at = stage
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    with pytest.raises(TimeoutError):
        await slack_media.upload_slack_files(
            files()[:1], request=transport.request, channel_id="C123",
        )
    assert len(transport.effects) == count
    assert [api for api, _ in transport.effects].count(stage) == 1


@pytest.mark.asyncio
async def test_cancellation_before_final_share_is_not_swallowed_or_retried(monkeypatch):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)

    async def authorize():
        if len(transport.effects) == 2:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await slack_media.upload_slack_files(
            files()[:1], request=transport.request, channel_id="C123", before_send=authorize,
        )
    assert [api for api, _ in transport.effects] == ["files.getUploadURLExternal", "bytes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"ok": False, "error": "missing_scope"},
    {"ok": True},
    {"ok": True, "files": []},
    {"ok": True, "files": [{"id": "UNKNOWN"}]},
    {"ok": True, "files": [{"id": "F1"}, {"id": "UNKNOWN"}]},
    {"ok": True, "files": [{"id": "F1"}, {"id": "F1"}]},
    {"ok": True, "files": [None]},
])
async def test_completion_requires_exact_unique_file_receipts(monkeypatch, response):
    transport = UploadTransport()
    transport.complete_response = response
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    with pytest.raises((RuntimeError, ValueError)):
        await slack_media.upload_slack_files(
            files()[:1], request=transport.request, channel_id="C123",
        )
    assert [api for api, _ in transport.effects].count("files.completeUploadExternal") == 1


@pytest.mark.asyncio
async def test_successful_file_receipt_does_not_require_a_permalink(monkeypatch):
    transport = UploadTransport()
    transport.complete_response = {"ok": True, "files": [{"id": "F1"}]}
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    result = await slack_media.upload_slack_files(
        files()[:1], request=transport.request, channel_id="D123",
    )
    assert result == (UploadedFile(id="F1", permalink=None),)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"ok": False, "error": "missing_scope"},
    {"ok": True, "upload_url": "https://files.slack.com/upload/example"},
    {"ok": True, "file_id": ""},
    {"ok": True, "file_id": "F1", "upload_url": None},
])
async def test_invalid_allocation_stops_before_transferring_bytes(monkeypatch, response):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    requests = []

    async def request(api, payload):
        requests.append((api, payload))
        return response

    with pytest.raises(RuntimeError):
        await slack_media.upload_slack_files(files(), request=request, channel_id="C123")
    assert len(requests) == 1 and requests[0][0] == "files.getUploadURLExternal"
    assert transport.effects == []


@pytest.mark.asyncio
async def test_duplicate_allocation_is_not_shared_or_uploaded_twice(monkeypatch):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    requests = []

    async def request(api, payload):
        requests.append((api, payload))
        return {"ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload/F1"}

    with pytest.raises(RuntimeError):
        await slack_media.upload_slack_files(files(), request=request, channel_id="C123")
    assert [api for api, _ in requests] == ["files.getUploadURLExternal"] * 2
    assert len(transport.effects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["", ".", "..", "../secret", "/tmp/secret", "a\\b", "a\nb"])
async def test_entire_batch_is_validated_before_allocating_files(monkeypatch, filename):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    batch = (files()[0], UploadFile(filename=filename, content=b"data"))
    with pytest.raises(ValueError):
        await slack_media.upload_slack_files(batch, request=transport.request, channel_id="C123")
    assert transport.effects == []


@pytest.mark.asyncio
async def test_empty_batch_cannot_finalize_or_publish_a_message():
    transport = UploadTransport()
    with pytest.raises(ValueError):
        await slack_media.upload_slack_files((), request=transport.request, channel_id="C123")
    assert transport.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [
    {"channel_id": ""}, {"channel_id": "C123\nCOTHER"}, {"channel_id": "https://example.com"},
    {"channel_id": "C123", "thread_ts": ""},
    {"channel_id": "C123", "thread_ts": "not-a-timestamp"},
])
async def test_invalid_destination_is_rejected_before_provider_calls(monkeypatch, destination):
    transport = UploadTransport()
    monkeypatch.setattr(slack_media, "upload_bytes", transport.upload)
    with pytest.raises(ValueError):
        await slack_media.upload_slack_files(files(), request=transport.request, **destination)
    assert transport.effects == []


class FakeHTTPResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("HTTP upload failed")


def http_transport(monkeypatch, status=200):
    calls = []

    class Session:
        def __init__(self, *args, **kwargs):
            calls.append(("session", args, kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return FakeHTTPResponse(status)

    monkeypatch.setattr(slack_media.aiohttp, "ClientSession", Session)
    return calls


@pytest.mark.asyncio
async def test_signed_upload_sends_only_raw_bytes_without_auth_or_redirects(monkeypatch):
    calls = http_transport(monkeypatch)
    url = "https://files.slack.com:443/upload/v1/example?signature=example"
    raw = b"\x00\xffrecording bytes"
    await slack_media.upload_bytes(url, raw)
    assert len(calls) == 2
    assert "auth" not in calls[0][2]
    assert "Authorization" not in calls[0][2].get("headers", {})
    assert calls[1] == ("post", url, {
        "data": raw, "headers": {"Content-Type": "application/octet-stream"},
        "allow_redirects": False,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://files.slack.com/upload/example",
    "https://files.slack.com.evil.example/upload/example",
    "https://evil.example/upload/example",
    "https://files.slack.com:444/upload/example",
    "https://user:secret@files.slack.com/upload/example",
    "https://files.slack.com/upload/example#fragment",
    "https://files.slack.com/other/example",
    "https://files.slack.com/uploaded/example",
])
async def test_signed_upload_rejects_untrusted_destination_before_http(monkeypatch, url):
    calls = http_transport(monkeypatch)
    with pytest.raises(ValueError):
        await slack_media.upload_bytes(url, b"private capture")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 400, 500])
async def test_only_exact_http_200_confirms_upload(monkeypatch, status):
    calls = http_transport(monkeypatch, status)
    with pytest.raises(RuntimeError):
        await slack_media.upload_bytes("https://files.slack.com/upload/example", b"recording")
    assert len([call for call in calls if call[0] == "post"]) == 1
    assert calls[-1][2]["allow_redirects"] is False


def test_rich_payload_has_accessible_fallback_and_no_transport_destination():
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": "Demo ready"}}]
    attachments = [{"color": "#5B3FD1", "blocks": blocks}]
    original_blocks, original_attachments = deepcopy(blocks), deepcopy(attachments)
    payload = slack_media.rich_payload(RichReply(
        text="Demo ready: https://example.com/pull/123",
        blocks=tuple(blocks), attachments=tuple(attachments),
    ))
    assert payload["text"] == "Demo ready: https://example.com/pull/123"
    expanded = [{**blocks[0], "expand": True}]
    assert payload["blocks"] == expanded
    assert payload["attachments"] == [{"color": "#5B3FD1", "blocks": expanded}]
    assert blocks == original_blocks and attachments == original_attachments
    assert not {"channel", "channel_id", "thread_ts", "token", "files"} & payload.keys()
    assert payload["unfurl_links"] is False and payload["unfurl_media"] is False


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_rich_reply_requires_readable_fallback_text(text):
    with pytest.raises(ValueError):
        slack_media.rich_payload(RichReply(text=text, blocks=({"type": "divider"},)))


def test_rich_reply_preserves_explicit_expansion_and_other_blocks():
    blocks = (
        {"type": "section", "expand": False,
         "text": {"type": "mrkdwn", "text": "Collapsed by choice"}},
        {"type": "section", "expand": True,
         "text": {"type": "mrkdwn", "text": "Expanded by choice"}},
        {"type": "divider"},
        {"type": "actions", "elements": [{"type": "button", "action_id": "view_pr",
            "text": {"type": "plain_text", "text": "View PR"},
            "url": "https://example.com/pull/123"}]},
    )
    attachments = ({"color": "#5B3FD1", "blocks": list(blocks)}, {"text": "Legacy card"})
    originals = deepcopy((blocks, attachments))
    payload = slack_media.rich_payload(RichReply("Fallback", blocks, attachments))
    assert payload["blocks"] == list(blocks)
    assert payload["attachments"] == list(attachments)
    assert (blocks, attachments) == originals


@pytest.mark.parametrize("attachments", [(), ({"color": "#5B3FD1", "text": "Details"},)])
def test_rich_reply_without_blocks_expands_its_text(attachments):
    payload = slack_media.rich_payload(RichReply("Full response", attachments=attachments))
    assert payload["blocks"] == [{"type": "section", "expand": True,
        "text": {"type": "mrkdwn", "text": "Full response", "verbatim": True}}]
    assert payload["text"] == "Full response"


@pytest.mark.parametrize("text", [
    "a" * 3000,
    "🌊测试" * 2500,
    "Line with *formatting*, `code`, and a <https://example.com|link>.\n" * 200,
])
def test_long_replies_fit_slack_sections_without_losing_text(text):
    blocks = slack_media.text_blocks(text)
    assert len(blocks) > 1
    assert all(block["type"] == "section" and block["expand"] is True for block in blocks)
    assert all(0 < len(block["text"]["text"]) <= 3000 for block in blocks)
    assert "".join(block["text"]["text"] for block in blocks) == text


def test_long_code_blocks_close_and_reopen_at_section_boundaries():
    code = "".join(f"print('Line {index}')\n" for index in range(500))
    text = f"Example:\n```{code}```\nDone."
    bodies = [block["text"]["text"] for block in slack_media.text_blocks(text)]
    assert len(bodies) > 2
    assert all(len(body) <= 3000 and body.count("```") % 2 == 0 for body in bodies)
    assert all(body.endswith("\n```") for body in bodies[:-1])
    assert all(body.startswith("```\n") for body in bodies[1:])
    # Remove only the added boundary fences and recover the exact original reply.
    recovered = bodies[0][:-4] + "".join(body[4:-4] for body in bodies[1:-1]) + bodies[-1][4:]
    assert recovered == text


@pytest.mark.parametrize("padding", [2989, 2990, 2991, 2992, 2993])
def test_chunk_boundary_never_splits_a_code_fence(padding):
    text = "a" * padding + "```code```" + "b" * 4000
    blocks = slack_media.text_blocks(text)
    assert all(block["text"]["text"].count("```") % 2 == 0 for block in blocks)
    assert all(len(block["text"]["text"]) <= 3000 for block in blocks)
    assert "".join(block["text"]["text"].replace("\n```", "").replace("```\n", "")
                   for block in blocks) == text


def test_generated_reply_blocks_respect_the_slack_message_limit():
    assert len(slack_media.text_blocks("a" * (2992 * 50))) == 50
    with pytest.raises(ValueError, match="at most 50"):
        slack_media.text_blocks("a" * (2992 * 50 + 1))
