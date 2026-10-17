"""Public native HTTP services with host-owned async transport and persistence."""
import asyncio
import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from agentchat.channels.slack.delivery import SlackDelivery
from agentchat.channels.slack.rendering import message_request
from agentchat.channels.slack.webhook import (
    SlackBodyTooLarge,
    SlackPayloadError,
    SlackSignatureError,
    SlackWebhook,
)
from agentchat.models import RichReply


def signed(raw, stamp="1000"):
    digest = hmac.new(b"secret", b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256)
    return {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": "v0=" + digest.hexdigest()}


def event_raw(**changes):
    payload = {"type": "event_callback", "team_id": "T1", "event_id": "Ev1", "event": {}}
    return json.dumps(payload | changes).encode()


def test_signed_event_challenge_and_ignored_envelope_keep_source():
    hook = SlackWebhook("secret")
    raw = event_raw()
    assert hook.event(raw, signed(raw), now=1000) == {"kind": "event", "payload": json.loads(raw)}
    raw = json.dumps({"type": "url_verification", "challenge": "proof"}).encode()
    assert hook.event(raw, signed(raw), now=1000)["challenge"] == "proof"
    raw = b'{"type":"app_rate_limited"}'
    assert hook.event(raw, signed(raw), now=1000)["kind"] == "ignored"


@pytest.mark.parametrize("change", [
    {"team_id": "T:injected"}, {"event_id": "Ev:other"}, {"event_id": "x" * 101},
    {"event_id": "é"}, {"event_id": False}, {"event_id": ""}, {"event": []},
])
def test_envelope_identity_and_structure_are_checked_before_admission(change):
    raw = event_raw(**change)
    with pytest.raises(SlackPayloadError):
        SlackWebhook("secret").event(raw, signed(raw), now=1000)


@pytest.mark.parametrize("raw", [b"[1]", b"invalid", b'\xff', b'{"type":"url_verification"}'])
def test_signed_invalid_json_or_envelope_is_a_payload_error(raw):
    with pytest.raises(SlackPayloadError):
        SlackWebhook("secret").event(raw, signed(raw), now=1000)


def test_signature_and_size_have_distinct_errors_and_no_secret_in_message():
    raw = event_raw()
    hook = SlackWebhook("secret")
    hook.verify(raw, signed(raw), now=1300)
    for headers, now, body in [(signed(raw), 1300.1, raw), (signed(raw), 699.9, raw),
                               ({}, 1000, raw), (signed(raw), 1000, b"tampered")]:
        with pytest.raises(SlackSignatureError, match="Invalid Slack signature"):
            hook.event(body, headers, now=now)
    with pytest.raises(SlackBodyTooLarge):
        hook.event(raw, signed(raw), now=1000, max_bytes=1)
    with pytest.raises(SlackSignatureError):
        SlackWebhook("").verify(raw, signed(raw), now=1000)


def test_interaction_uses_the_signed_form_bytes_and_preserves_unicode():
    payload = {"type": "block_actions", "team": {"id": "T1"}, "value": "café + &"}
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    assert SlackWebhook("secret").interaction(raw, signed(raw), now=1000) == payload


@pytest.mark.parametrize("raw", [
    b"payload=%7B%7D&payload=%7B%7D", b"payload=[]", b"other={}", b"payload=%GG",
    b"payload=%7B%22x%22%3A%22%FF%22%7D", b"payload={}&bad", b"payload=%",
])
def test_interaction_rejects_ambiguous_or_malformed_payload(raw):
    with pytest.raises(SlackPayloadError):
        SlackWebhook("secret").interaction(raw, signed(raw), now=1000)


@pytest.mark.asyncio
async def test_reply_and_update_use_existing_request_plan_once():
    calls = []

    async def request(verb, method, payload):
        calls.append((verb, method, payload))
        return {"ok": True, "ts": payload.get("ts", "123.002"), "channel": payload["channel"]}

    service = SlackDelivery(request)
    result = await service.send(RichReply("Ready"), channel_id="C123", thread_ts="123.001")
    assert result["status"] == "confirmed"
    assert len(calls) == 1 and calls[0][:2] == ("POST", "chat.postMessage")
    plan = message_request(RichReply("Complete"), channel_id="C123", message_ts="123.002",
                           fallback_blocks=False)
    result = await service.send_request(plan)
    assert result["status"] == "confirmed"
    assert calls[-1] == ("POST", "chat.update", plan["payload"])
    assert calls[-1][2]["blocks"] == [] and calls[-1][2]["attachments"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response,status", [
    ({"ok": True, "ts": "123.002"}, "confirmed"),
    ({"ok": False, "error": "missing_scope"}, "rejected"),
    ({"ok": True}, "uncertain"), ({"ok": "yes", "ts": "123.002"}, "uncertain"),
    ({"ok": True, "ts": "not-a-timestamp"}, "uncertain"),
    ({"ok": True, "ts": "123.002", "channel": "COTHER"}, "uncertain"),
    ([], "uncertain"),
])
async def test_send_classifies_only_confirmed_receipts(response, status):
    calls = []

    async def request(*args):
        calls.append(args)
        return response

    result = await SlackDelivery(request).send(RichReply("Ready"), channel_id="C123")
    assert result["status"] == status and len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_plan_fails_before_transport():
    calls = []

    async def request(*args):
        calls.append(args)
        return {"ok": True}

    with pytest.raises(ValueError):
        await SlackDelivery(request).send_request({"method": "chat.delete", "payload": {}})
    assert calls == []


@pytest.mark.asyncio
async def test_uncertain_write_is_not_retried_and_authority_errors_propagate():
    calls = []

    async def lost_response(*args):
        calls.append(args)
        raise ConnectionError("lost response")

    result = await SlackDelivery(lost_response).send(RichReply("Ready"), channel_id="C123")
    assert result["status"] == "uncertain" and len(calls) == 1

    async def denied(*_args):
        raise PermissionError("authority changed")

    with pytest.raises(PermissionError, match="authority changed"):
        await SlackDelivery(denied).send(
            RichReply("Ready"), channel_id="C123",
        )


def receipt(identity="one", kind="delivery", thread="123.001"):
    return {"ts": "123.002", "thread_ts": thread,
            "metadata": {"event_type": kind, "event_payload": {"id": identity}}}


@pytest.mark.asyncio
async def test_reconciliation_paginates_and_matches_both_metadata_fields_and_thread():
    calls = []

    async def request(verb, method, payload):
        calls.append((verb, method, payload))
        if "cursor" not in payload:
            return {"ok": True, "messages": [receipt(kind="forged"), receipt("other"),
                                             receipt(thread="999.001")],
                    "response_metadata": {"next_cursor": "second"}}
        return {"ok": True, "messages": [receipt()]}

    result = await SlackDelivery(request).reconcile("C123", "123.001", event_type="delivery",
                                                    event_id="one")
    assert result["status"] == "confirmed"
    assert len(calls) == 2
    assert all(call[:2] == ("GET", "conversations.replies") for call in calls)
    assert calls[-1][2]["cursor"] == "second"


@pytest.mark.asyncio
@pytest.mark.parametrize("response,reason", [
    ({"ok": True, "messages": []}, "not_found"),
    ({"ok": False, "error": "missing_scope"}, "history_unavailable"),
    ({"ok": True, "messages": [], "has_more": True}, "missing_cursor"),
    ({"ok": True, "messages": [], "response_metadata": {"next_cursor": "next"}}, "page_limit"),
])
async def test_read_failure_or_absence_never_proves_write_failed(response, reason):
    calls = []

    async def request(*args):
        calls.append(args)
        return response

    result = await SlackDelivery(request).reconcile("C123", "123.001", event_type="delivery",
                                                    event_id="one", max_pages=1)
    assert result["status"] == "uncertain" and result["reason"] == reason and len(calls) == 1


@pytest.mark.asyncio
async def test_reconciliation_is_bounded_and_concurrent_calls_do_not_share_cursors():
    async def request(_verb, _method, payload):
        await asyncio.sleep(0)
        if "cursor" not in payload:
            return {"ok": True, "messages": [],
                    "response_metadata": {"next_cursor": payload["channel"]}}
        assert payload["cursor"] == payload["channel"]
        return {"ok": True, "messages": [receipt(payload["channel"])]}

    service = SlackDelivery(request)
    results = await asyncio.gather(*(
        service.reconcile(channel, "123.001", event_type="delivery", event_id=channel)
        for channel in ("C123", "C456")
    ))
    assert all(result["status"] == "confirmed" for result in results)

    async def repeated(*_args):
        return {"ok": True, "messages": [], "response_metadata": {"next_cursor": "same"}}

    result = await SlackDelivery(repeated).reconcile(
        "C123", "123.001", event_type="delivery", event_id="one",
    )
    assert result["reason"] == "repeated_cursor"


@pytest.mark.asyncio
async def test_read_timeout_cancels_callback_and_cancellation_of_write_propagates():
    cancelled = asyncio.Event()

    async def waiting(*_args):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    service = SlackDelivery(waiting)
    result = await service.reconcile(
        "C123", "123.001", event_type="delivery", event_id="one", timeout=.01,
    )
    assert result["reason"] == "timeout" and cancelled.is_set()
    cancelled.clear()
    task = asyncio.create_task(service.send(RichReply("Ready"), channel_id="C123"))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_working_status_uses_shared_plan_without_message_receipt_or_retry():
    calls = []

    async def request(*args):
        calls.append(args)
        return {"ok": True}

    result = await SlackDelivery(request).set_status("C123", "123.001", "Working")
    assert result["status"] == "confirmed"
    assert calls == [("POST", "assistant.threads.setStatus",
                      {"channel_id": "C123", "thread_ts": "123.001", "status": "Working"})]
