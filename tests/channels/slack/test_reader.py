"""Public host reader parity: verified bytes, bounded retries and inert Canvas views."""

import asyncio
import tempfile
import threading
from contextlib import asynccontextmanager
from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from agentchat.channels.slack import files

FILE_ID = "F12345678"
URL = "https://files.slack.com/files-pri/T123-F12345678/notes.txt"


def metadata(**overrides):
    return {
        "ok": True,
        "file": {
            "id": FILE_ID,
            "name": "notes.txt",
            "mimetype": "text/plain",
            "size": 5,
            "url_private": URL,
            **overrides,
        },
    }


def setup(monkeypatch, *, chunks=(b"hello",), status=200, headers=None, info=None):
    calls = []
    temporaries = []
    real_tempfile = tempfile.TemporaryFile

    def temporary():
        raw = real_tempfile()
        temporaries.append(raw)
        return raw

    class Response:
        def __init__(self):
            self.status = status
            self.headers = headers if headers is not None else {"Content-Length": "5"}
            self.content = self

        async def iter_chunked(self, size):
            assert size == 64 * 1024
            for chunk in chunks:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk

    class Client:
        @asynccontextmanager
        async def get(self, url, *, headers, allow_redirects):
            assert url == URL
            assert headers == {"Authorization": "Bearer fresh-token", "Accept-Encoding": "identity"}
            assert allow_redirects is False
            calls.append("download")
            yield Response()

    @asynccontextmanager
    async def session(*, timeout, auto_decompress):  # noqa: ASYNC109
        assert (timeout.total, timeout.sock_connect, timeout.sock_read) == (45, 10, 15)
        assert auto_decompress is False
        yield Client()

    async def request(method, payload, token):
        assert (method, payload, token) == ("files.info", {"file": FILE_ID}, "fresh-token")
        calls.append("metadata")
        return metadata() if info is None else info

    monkeypatch.setattr(files.aiohttp, "ClientSession", session)
    monkeypatch.setattr(tempfile, "TemporaryFile", temporary)
    return (
        {
            "token": AsyncMock(return_value="fresh-token"),
            "request": request,
            "authorize": AsyncMock(),
        },
        calls,
        temporaries,
    )


@pytest.mark.asyncio
async def test_verified_file_is_seekable_and_owned_by_caller(monkeypatch):
    callbacks, calls, temporaries = setup(monkeypatch, chunks=(b"he", b"llo"))
    result = await files.SlackFileReader(max_bytes=100).read(FILE_ID, **callbacks)
    try:
        assert (result.name, result.media_type, result.raw.read()) == (
            "notes.txt",
            "text/plain",
            b"hello",
        )
        assert result.state["received_bytes"] == 5
        assert calls == ["metadata", "download"]
        assert callbacks["authorize"].await_count == 4
        assert not temporaries[0].closed
    finally:
        result.raw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status", "chunks", "code"),
    [
        ({"Content-Length": "4"}, 200, (b"hello",), "metadata_size_mismatch"),
        ({"Content-Length": "5"}, 200, (b"he",), "truncated_stream"),
        ({"Content-Length": "5"}, 200, (b"helloo",), "transfer_size_mismatch"),
        ({}, 200, (b"helloo",), "metadata_size_mismatch"),
        ({"Content-Length": "five"}, 200, (b"hello",), "invalid_headers"),
        ({"Content-Encoding": "gzip"}, 200, (b"hello",), "encoded_response"),
        ({"Content-Encoding": "secret-provider-header"}, 200, (b"hello",), "encoded_response"),
        ({"Content-Range": "bytes 0-4/10"}, 200, (b"hello",), "partial_response"),
        ({}, 206, (b"hello",), "partial_response"),
        ({}, 302, (b"hello",), "redirect"),
        ({}, 403, (b"hello",), "authentication"),
        ({}, 503, (b"hello",), "transient_http"),
    ],
)
async def test_integrity_failures_do_not_publish_partial_files(
    monkeypatch, headers, status, chunks, code
):
    callbacks, _, temporaries = setup(monkeypatch, headers=headers, status=status, chunks=chunks)
    with pytest.raises(files.SlackFileError) as caught:
        await files.SlackFileReader(max_bytes=100, read_attempts=1).read(FILE_ID, **callbacks)
    assert caught.value.code == code
    assert all(raw.closed for raw in temporaries)
    assert "secret-provider-header" not in str(caught.value)
    assert caught.value.diagnostic()["attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"id": "FOTHER"},
        {"is_external": True},
        {"size": True},
        {"size": 0},
        {"url_private": "https://attacker.test/files-pri/x"},
        {"url_private": "https://token@files.slack.com/files-pri/x"},
        {"url_private": "https://files.slack.com/files-pri/x#fragment"},
    ],
)
async def test_rejects_unverifiable_metadata_before_download(monkeypatch, change):
    callbacks, calls, _ = setup(monkeypatch, info=metadata(**change))
    with pytest.raises(files.SlackFileError):
        await files.SlackFileReader(max_bytes=100).read(FILE_ID, **callbacks)
    assert calls == ["metadata"]


@pytest.mark.asyncio
async def test_retries_get_failure_but_never_uncertain_credential_refresh(monkeypatch):
    callbacks, calls, _ = setup(monkeypatch, status=503)
    with pytest.raises(files.SlackFileError) as caught:
        await files.SlackFileReader(max_bytes=100, retry_delay=0).read(FILE_ID, **callbacks)
    assert calls == ["metadata", "download", "metadata", "download"]
    assert len(caught.value.history) == 2
    callbacks["token"].side_effect = files.SlackTransportError(
        "response_unavailable", retryable=True, dispatched=True
    )
    calls.clear()
    with pytest.raises(files.SlackFileError) as caught:
        await files.SlackFileReader(max_bytes=100, retry_delay=0).read(FILE_ID, **callbacks)
    assert caught.value.code == "credential_unconfirmed"
    assert caught.value.diagnostic()["attempts"] == 1
    assert "did not replay" in caught.value.retry_blocker
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", [None, "NaN", "-1", "300"])
async def test_never_shortens_provider_retry_interval(monkeypatch, retry_after):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    callbacks, calls, _ = setup(monkeypatch, headers=headers, status=429)
    reader = files.SlackFileReader(max_bytes=100, retry_delay=0, read_budget=1)
    with pytest.raises(files.SlackFileError) as caught:
        await reader.read(FILE_ID, **callbacks)
    assert caught.value.code == "rate_limited"
    assert caught.value.diagnostic()["attempts"] == 1
    assert "no early retry" in caught.value.retry_blocker
    assert calls == ["metadata", "download"]
    if retry_after == "300":
        with pytest.raises(files.SlackFileError):
            await reader.read(FILE_ID, **callbacks)
        assert calls == ["metadata", "download"]


@pytest.mark.asyncio
async def test_access_revocation_closes_verified_tempfile(monkeypatch):
    callbacks, _, temporaries = setup(monkeypatch)
    callbacks["authorize"].side_effect = [None, None, None, files.SlackFileError("access_changed")]
    with pytest.raises(files.SlackFileError, match="access_changed"):
        await files.SlackFileReader(max_bytes=100).read(FILE_ID, **callbacks)
    assert temporaries[0].closed


@pytest.mark.asyncio
async def test_whole_read_budget_is_bounded(monkeypatch):
    callbacks, _, _ = setup(monkeypatch)

    async def slow_token():
        await asyncio.sleep(10)

    callbacks["token"] = slow_token
    with pytest.raises(files.SlackFileError) as caught:
        await files.SlackFileReader(max_bytes=100, read_budget=0.01).read(FILE_ID, **callbacks)
    assert caught.value.code == "read_budget"
    assert caught.value.diagnostic()["stage"] == "access"


@pytest.mark.asyncio
async def test_cancelled_file_worker_finishes_before_caller_can_close():
    started, finish = threading.Event(), threading.Event()
    raw = BytesIO()

    def write():
        started.set()
        assert finish.wait(2)
        raw.write(b"completed")

    task = asyncio.create_task(files.file_operation(write))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw.getvalue() == b"completed"


@pytest.mark.asyncio
async def test_canvas_export_size_is_independent_and_original_is_retained(monkeypatch):
    body = (
        b'<div class="quip-canvas-content"><h1>Plan &amp; notes</h1><p>Visible</p>'
        b"<script>secret</script><p hidden>hidden</p></div>"
    )
    callbacks, _, _ = setup(
        monkeypatch,
        info=metadata(mimetype=files.CANVAS_MIME, filetype="quip", mode="quip", size=99999),
        chunks=(body,),
        headers={"Content-Length": str(len(body)), "Content-Type": "text/html; charset=utf-8"},
    )
    result = await files.SlackFileReader(max_bytes=1000).read(FILE_ID, **callbacks)
    try:
        assert result.name == "notes.txt.html"
        assert result.representation == "canvas_html"
        assert files.inspect_canvas(result.raw) == (
            files.CANVAS_MIME,
            b"",
            "# Plan & notes\n\nVisible",
        )
        assert result.raw.read() == body
    finally:
        result.raw.close()


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Sign in</html>",
        b'<div class="quip-canvas-content">unfinished',
        b'<div class="quip-canvas-content"><form>login</form></div>',
        b'<div class="quip-canvas-content">\xff</div>',
    ],
)
def test_invalid_canvas_is_not_document_content(body):
    raw = BytesIO(body)
    with pytest.raises(files.CanvasFormatError):
        files.inspect_canvas(raw)
    assert raw.tell() == 0


def test_canvas_preview_bounds_source_output_and_hidden_content():
    body = b'<div class="quip-canvas-content">' + b"hello " * 50000 + b"</div>"
    raw = BytesIO(body)
    mime, _, text = files.inspect_canvas(raw)
    assert mime == files.CANVAS_MIME
    assert len(text) <= 8000
    assert text.endswith("retained in the attachment.]")
    assert raw.read() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermissionError("revoked"), TimeoutError("authority timed out")])
async def test_authority_failures_propagate_without_network_or_retry(monkeypatch, error):
    callbacks, calls, _ = setup(monkeypatch)
    callbacks["authorize"].side_effect = error
    with pytest.raises(type(error)) as caught:
        await files.SlackFileReader(max_bytes=100, retry_delay=0).read(FILE_ID, **callbacks)
    assert caught.value is error
    assert calls == []
    assert callbacks["authorize"].await_count == 1


@pytest.mark.asyncio
async def test_budget_failure_retains_earlier_attempts(monkeypatch):
    callbacks, _, _ = setup(monkeypatch, status=503)
    tokens = 0

    async def token():
        nonlocal tokens
        tokens += 1
        if tokens == 2:
            await asyncio.sleep(10)
        return "fresh-token"

    callbacks["token"] = token
    with pytest.raises(files.SlackFileError) as caught:
        await files.SlackFileReader(max_bytes=100, read_budget=0.02, retry_delay=0).read(
            FILE_ID, **callbacks
        )
    assert [attempt["code"] for attempt in caught.value.history] == [
        "transient_http",
        "read_budget",
    ]
    assert caught.value.history[-1]["attempt"] == 2


@pytest.mark.asyncio
async def test_very_large_retry_after_is_safe_and_never_retried_early(monkeypatch):
    callbacks, calls, _ = setup(monkeypatch, status=429, headers={"Retry-After": "1e300"})
    with pytest.raises(files.SlackFileError, match="no early retry"):
        await files.SlackFileReader(max_bytes=100).read(FILE_ID, **callbacks)
    assert calls == ["metadata", "download"]
