"""Public host API: use real native policies with a credential-free page callback."""
import asyncio
import json

import pytest

from agentchat.channels.slack.events import to_message
from agentchat.channels.slack.host import SlackHost

BOT = "UBOT00001"
ORIGIN = {
    "team_id": "TTEST0001", "channel_id": "CTEST0001",
    "thread_ts": "1780000000.000001", "message_ts": "1780000000.000010",
}
URL = "https://x.com/deepseek_ai/status/2111375640717033777?s=46"
QUESTION = "do we support this?"
HOST = SlackHost(bot_user_id=BOT, workspace_id=ORIGIN["team_id"],
                 exclude_prefixes=("!aside", "(aside)"))


def message(ts="1780000000.000001", text="new deepseek", **extra):
    return {"ts": ts, "user": "UAUTHOR01", "text": text, **extra}


def page(messages, cursor=None):
    return {"ok": True, "messages": messages, "has_more": cursor is not None,
            "response_metadata": {"next_cursor": cursor or ""}}


def event(text=f"<@{BOT}> {QUESTION}", **extra):
    return {"team_id": ORIGIN["team_id"], "event": {
        "type": "message", "channel_type": "channel", "channel": ORIGIN["channel_id"],
        "ts": ORIGIN["message_ts"], "thread_ts": ORIGIN["thread_ts"],
        "user": "UAUTHOR01", "text": text, **extra,
    }}


@pytest.mark.asyncio
async def test_parent_link_unfurl_and_current_extras_survive_without_repeating_question():
    payload = event()
    normalized = HOST.normalize_event(payload)
    assert normalized and normalized["text"] == QUESTION and normalized["mentioned"]
    calls = []

    async def request(params):
        calls.append(params)
        return page([
            message(text=f"{URL} new deepseek", attachments=[{
                "title": "DeepSeek (@deepseek_ai) on X", "title_link": URL,
                "text": "DeepSeek-V4.1-Flash-1017 is here!", "image_url": "https://example.com/chart.png",
            }]),
            message(ORIGIN["message_ts"], f"<@{BOT}> {QUESTION}", files=[{"id": "FFILE0001"}],
                    blocks=[{"type": "rich_text", "elements": [{"text": QUESTION}]}],
                    attachments=[{"text": "current link preview"}]),
        ])

    context = await HOST.thread_context(normalized, request)
    references = "\n".join(item["content"] for item in context["messages"])
    assert context["status"] == "complete"
    assert URL in references and "DeepSeek-V4.1-Flash-1017" in references
    assert "current link preview" in references
    assert QUESTION not in references and "FFILE0001" not in references
    assert context["messages"][0]["author"] == "UAUTHOR01"
    assert calls == [{"channel": ORIGIN["channel_id"], "ts": ORIGIN["thread_ts"],
                      "latest": ORIGIN["message_ts"], "inclusive": True, "limit": 100,
                      "cursor": "", "include_all_metadata": True}]


@pytest.mark.parametrize("quoted", [
    f"`<@{BOT}>`", f"```\n<@{BOT}>\n```", f"> <@{BOT}>", f">>> <@{BOT}>\nrest",
    f"`<@{BOT}>", f"```<@{BOT}>",
])
def test_quoted_mentions_are_literal_in_host_and_existing_event_api(quoted):
    payload = event(quoted)
    normalized = HOST.normalize_event(payload)
    legacy = to_message(payload, payload["event"], bot_user_id=BOT,
                        workspace_id=ORIGIN["team_id"])
    assert normalized and normalized["text"] == quoted and not normalized["mentioned"]
    assert legacy and legacy.text == quoted and not legacy.addressed
    routed = HOST.routing_text(f"<@{BOT}> explain\n{quoted}")
    assert routed == {"text": f"explain\n{quoted}", "mentioned": True, "excluded": False}


@pytest.mark.parametrize("updates", [
    {"text": 1}, {"channel": "not-a-channel"}, {"ts": "NaN"},
    {"channel_type": {}}, {"subtype": {}},
    {"thread_ts": "1780000001.000001"},
])
def test_invalid_supported_event_is_rejected(updates):
    with pytest.raises(ValueError):
        HOST.normalize_event(event(**updates))


def test_ignored_events_and_explicit_dm_routing():
    assert HOST.normalize_event(event("!ASIDE private thought")) is None
    assert HOST.normalize_event(event(subtype="message_changed")) is None
    assert HOST.normalize_event(event(bot_id="BBOT00001")) is None
    assert HOST.normalize_event(event(bot_id=None, bot_profile=None)) is not None
    assert HOST.normalize_event(event(channel="DTEST0001", channel_type=None)) is None
    direct = HOST.normalize_event(event(channel="DTEST0001", channel_type="im"))
    assert direct and direct["direct"]
    assert not HOST.routing_text("!asideways")['excluded']


@pytest.mark.asyncio
async def test_paginated_history_excludes_future_other_threads_asides_and_duplicates():
    calls = []

    async def request(params):
        calls.append(params["cursor"])
        if not params["cursor"]:
            return page([message(), message("1780000000.000002", "prior"),
                         message("1780000000.000003", "!aside hidden"),
                         message("1780000000.000004", "foreign", thread_ts="1780000000.000009"),
                         message("1780000000.000011", "future")], "next")
        return page([message(), message("1780000000.000005", "recent")])

    context = await HOST.thread_context(ORIGIN, request)
    assert calls == ["", "next"]
    assert context["status"] == "complete"
    assert [json.loads(item["content"])["text"] for item in context["messages"]] == [
        "new deepseek", "prior", "recent",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_page", [True, False])
async def test_provider_failure_retains_good_pages_and_reports_coverage(first_page):
    calls = 0

    async def request(_):
        nonlocal calls
        calls += 1
        if not first_page and calls == 1:
            return page([message()], "next")
        return {"ok": False, "error": "missing_scope"}

    context = await HOST.thread_context(ORIGIN, request)
    assert context["status"] == ("unavailable" if first_page else "partial")
    assert context["error"] == "missing_scope"
    assert len(context["messages"]) == (0 if first_page else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [ORIGIN, {**ORIGIN, "message_ts": ORIGIN["thread_ts"]}])
async def test_missing_root_is_never_complete(origin):
    async def request(_):
        return page([])

    context = await HOST.thread_context(origin, request)
    assert context["status"] == "partial" and "root" in context["warning"]


@pytest.mark.asyncio
async def test_timeout_cancels_callback_and_authorization_errors_propagate():
    cancelled = asyncio.Event()

    async def slow(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context = await HOST.thread_context(ORIGIN, slow, timeout=0.01)
    assert context["status"] == "unavailable" and context["error"] == "timeout"
    assert cancelled.is_set()

    async def denied(_):
        raise PermissionError("authorization revoked")

    with pytest.raises(PermissionError, match="authorization revoked"):
        await HOST.thread_context(ORIGIN, denied)


@pytest.mark.asyncio
async def test_page_and_character_limits_keep_root_and_recent_context():
    calls = []

    async def request(params):
        calls.append(params)
        return page([message(text="root"), message("1780000000.000002", "old"),
                     message("1780000000.000003", "new")], "more")

    context = await HOST.thread_context(ORIGIN, request, max_pages=1, max_chars=29)
    assert len(calls) == 1 and context["status"] == "partial"
    assert [json.loads(item["content"])["text"] for item in context["messages"]] == ["root", "new"]
    assert sum(len(item["content"]) for item in context["messages"]) <= 29


@pytest.mark.asyncio
@pytest.mark.parametrize("response, status, reason", [
    ({"ok": True, "messages": "bad"}, "unavailable", "invalid_response"),
    ({"ok": True, "messages": [message()]}, "partial", "pagination"),
    ({**page([message()], ""), "has_more": True}, "partial", "cursor"),
])
async def test_malformed_page_coverage_is_explicit(response, status, reason):
    async def request(_):
        return response

    context = await HOST.thread_context(ORIGIN, request)
    assert context["status"] == status
    assert reason in context.get("error", context.get("warning", ""))


@pytest.mark.asyncio
async def test_timeout_bounds_the_whole_read_and_keeps_first_page():
    calls = 0

    async def request(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return page([message()], "next")
        await asyncio.Event().wait()

    context = await HOST.thread_context(ORIGIN, request, timeout=0.01)
    assert calls == 2 and context["status"] == "partial"
    assert context["error"] == "timeout" and len(context["messages"]) == 1


@pytest.mark.asyncio
async def test_individual_reference_truncation_is_visible_and_unicode_bounded():
    async def request(_):
        return page([message(text="🗿" * 100)])

    context = await HOST.thread_context(ORIGIN, request, max_message_chars=20)
    assert context["status"] == "partial"
    assert context["messages"][0]["truncated"]
    assert len(context["messages"][0]["content"]) == 20


def test_existing_event_api_preserves_unfurls_and_noncomposer_blocks():
    payload = event(
        attachments=[{"title_link": URL, "text": "DeepSeek-V4.1-Flash-1017"}],
        blocks=[{"type": "rich_text", "text": QUESTION},
                {"type": "image", "image_url": "https://example.com/chart.png"}],
    )
    converted = to_message(payload, payload["event"], bot_user_id=BOT,
                           workspace_id=ORIGIN["team_id"])
    assert converted is not None
    reference = converted.metadata["slack_reference"]
    assert reference["attachments"][0]["title_link"] == URL
    assert reference["blocks"] == [{"type": "image", "image_url": "https://example.com/chart.png"}]
    assert converted.text == QUESTION


@pytest.mark.asyncio
async def test_current_event_previews_survive_without_history_and_on_provider_failure():
    source = HOST.normalize_event(event(
        channel="DTEST0001", channel_type="im",
        attachments=[{"title_link": URL, "text": "DeepSeek-V4.1-Flash-1017"}],
    ))
    assert source is not None
    local = await HOST.thread_context(source)
    assert local["status"] == "partial" and local["error"] == "history_not_requested"
    assert local["messages"][0]["author"] == source["user_id"]
    assert URL in local["messages"][0]["content"]
    assert QUESTION not in local["messages"][0]["content"]

    async def denied(_):
        return {"ok": False, "error": "missing_scope"}

    failed = await HOST.thread_context(source, denied)
    assert failed["status"] == "partial" and failed["error"] == "missing_scope"
    assert failed["messages"] == local["messages"]
    empty = await HOST.thread_context(ORIGIN)
    assert empty["status"] == "unavailable" and empty["error"] == "history_not_requested"


@pytest.mark.asyncio
async def test_seeded_and_fetched_current_previews_are_merged_once():
    preview = {"title_link": URL, "text": "DeepSeek-V4.1-Flash-1017"}
    source = HOST.normalize_event(event(attachments=[preview]))
    assert source is not None

    async def request(_):
        return page([message(), message(ORIGIN["message_ts"], QUESTION,
                                       attachments=[preview, {"text": "new preview"}])])

    context = await HOST.thread_context(source, request)
    assert context["status"] == "complete"
    current = context["messages"][-1]
    assert current["ts"] == ORIGIN["message_ts"]
    assert current["content"].count("DeepSeek-V4.1-Flash-1017") == 1
    assert "new preview" in current["content"]
    assert QUESTION not in current["content"]
    assert len(context["messages"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field, changed", [
    ("team_id", "TOTHER001"), ("channel_id", "COTHER001"),
    ("thread_ts", "1780000000.000002"), ("message_ts", "1780000000.000009"),
    ("user_id", "UOTHER001"),
])
async def test_reference_from_another_event_is_rejected_before_callback(field, changed):
    source = HOST.normalize_event(event(attachments=[{"text": "private preview"}]))
    assert source is not None
    source["reference"][field] = changed
    calls = []

    async def request(params):
        calls.append(params)
        return page([])

    with pytest.raises(ValueError, match="different event"):
        await HOST.thread_context(source, request)
    assert not calls


@pytest.mark.asyncio
async def test_acknowledgement_uses_the_thread_reply_message_timestamp():
    source = HOST.normalize_event(event())
    assert source is not None
    calls = []

    async def request(api_method, payload):
        calls.append((api_method, payload))
        return {"ok": True}

    assert await HOST.acknowledge(source, request) is True
    assert calls == [(
        "reactions.add",
        {"channel": ORIGIN["channel_id"], "name": "eyes", "timestamp": ORIGIN["message_ts"]},
    )]


@pytest.mark.asyncio
async def test_acknowledgement_is_disabled_without_an_emoji():
    source = HOST.normalize_event(event())
    assert source is not None
    calls = []

    async def request(api_method, payload):
        calls.append((api_method, payload))
        return {"ok": True}

    host = SlackHost(bot_user_id=BOT, workspace_id=ORIGIN["team_id"], ack_emoji=None)
    assert await host.acknowledge(source, request) is False
    assert calls == []


@pytest.mark.asyncio
async def test_acknowledgement_accepts_already_reacted_and_rejects_provider_errors():
    source = HOST.normalize_event(event())
    assert source is not None

    async def already_reacted(api_method, payload):
        return {"ok": False, "error": "already_reacted"}

    async def missing_scope(api_method, payload):
        return {"ok": False, "error": "missing_scope"}

    assert await HOST.acknowledge(source, already_reacted) is True
    assert await HOST.acknowledge(source, missing_scope) is False


@pytest.mark.asyncio
async def test_acknowledgement_swallows_callback_exceptions_and_timeouts():
    source = HOST.normalize_event(event())
    assert source is not None

    async def raising(api_method, payload):
        raise PermissionError("authorization revoked")

    async def slow(api_method, payload):
        await asyncio.Event().wait()

    assert await HOST.acknowledge(source, raising) is False
    assert await HOST.acknowledge(source, slow, timeout=0.01) is False


@pytest.mark.asyncio
async def test_acknowledgement_cancellation_propagates():
    source = HOST.normalize_event(event())
    assert source is not None
    started = asyncio.Event()

    async def blocked(api_method, payload):
        started.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(HOST.acknowledge(source, blocked))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.asyncio
async def test_acknowledgement_rejects_invalid_event_before_callback():
    source = HOST.normalize_event(event())
    assert source is not None
    invalid = dict(source)
    invalid.pop("message_ts")
    calls = []

    async def request(api_method, payload):
        calls.append((api_method, payload))
        return {"ok": True}

    with pytest.raises(ValueError, match="Invalid Slack event origin"):
        await HOST.acknowledge(invalid, request)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_value", [0, -1, float("inf"), float("nan")])
async def test_acknowledgement_rejects_nonpositive_or_nonfinite_timeout(timeout_value):
    source = HOST.normalize_event(event())
    assert source is not None

    async def request(api_method, payload):
        return {"ok": True}

    with pytest.raises(ValueError, match="positive finite timeout"):
        await HOST.acknowledge(source, request, timeout=timeout_value)
