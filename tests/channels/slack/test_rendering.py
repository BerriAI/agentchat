from copy import deepcopy

import pytest

from agentchat.channels.slack import rendering
from agentchat.models import RichReply


def test_rich_payload_has_accessible_fallback_and_no_transport_destination():
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": "Demo ready"}}]
    attachments = [{"color": "#5B3FD1", "blocks": blocks}]
    original_blocks, original_attachments = deepcopy(blocks), deepcopy(attachments)
    payload = rendering.rich_payload(RichReply(
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
        rendering.rich_payload(RichReply(text=text, blocks=({"type": "divider"},)))


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
    payload = rendering.rich_payload(RichReply("Fallback", blocks, attachments))
    assert payload["blocks"] == list(blocks)
    assert payload["attachments"] == list(attachments)
    assert (blocks, attachments) == originals


@pytest.mark.parametrize("attachments", [(), ({"color": "#5B3FD1", "text": "Details"},)])
def test_rich_reply_without_blocks_expands_its_text(attachments):
    payload = rendering.rich_payload(RichReply("Full response", attachments=attachments))
    assert payload["blocks"] == [{"type": "section", "expand": True,
        "text": {"type": "mrkdwn", "text": "Full response", "verbatim": True}}]
    assert payload["text"] == "Full response"


@pytest.mark.parametrize("text", [
    "a" * 3000,
    "🌊测试" * 2500,
    "Line with *formatting*, `code`, and a <https://example.com|link>.\n" * 200,
])
def test_long_replies_fit_slack_sections_without_losing_text(text):
    blocks = rendering.text_blocks(text)
    assert len(blocks) > 1
    assert all(block["type"] == "section" and block["expand"] is True for block in blocks)
    assert all(0 < len(block["text"]["text"]) <= 3000 for block in blocks)
    assert "".join(block["text"]["text"] for block in blocks) == text


def test_long_code_blocks_close_and_reopen_at_section_boundaries():
    code = "".join(f"print('Line {index}')\n" for index in range(500))
    text = f"Example:\n```{code}```\nDone."
    bodies = [block["text"]["text"] for block in rendering.text_blocks(text)]
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
    blocks = rendering.text_blocks(text)
    assert all(block["text"]["text"].count("```") % 2 == 0 for block in blocks)
    assert all(len(block["text"]["text"]) <= 3000 for block in blocks)
    assert "".join(block["text"]["text"].replace("\n```", "").replace("```\n", "")
                   for block in blocks) == text


def test_generated_reply_blocks_respect_the_slack_message_limit():
    assert len(rendering.text_blocks("a" * (2992 * 50))) == 50
    with pytest.raises(ValueError, match="at most 50"):
        rendering.text_blocks("a" * (2992 * 50 + 1))
