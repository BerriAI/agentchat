import pytest

from agentchat.channels.slack.rendering import markdown_text, split_text, text_blocks


@pytest.mark.parametrize(("text", "expected"), [
    ("**see `x` now** **", "*see `x` now* **"),
    ("## **Model** `name` is **available**", "*Model `name` is available*"),
    ("# Glob: **/*.py", "*Glob: **/*.py*"),
    ("# Math: 2**10", "*Math: 2**10*"),
    ("#### `**literal**` and **bold**", "*`**literal**` and bold*"),
    ("###   \nNormal", "###   \nNormal"),
    ("# \x1ctest", "# \x1ctest"),
    ("[text](https://example.com/\x1cfile)", "[text](https://example.com/\x1cfile)"),
    ("```python\r\n## **literal**\n`x`", "```\n## **literal**\n`x`"),
    ("# [Docs](https://example.com)", "*<https://example.com|Docs>*"),
])
def test_markdown_headings_bold_and_unclosed_fences(text, expected):
    assert markdown_text(text) == expected


def test_local_resolution_preserves_authority_and_excludes_fenced_code():
    requested = []

    def resolve(target):
        requested.append(target)
        return f"https://app.test/?file={target}&view=1" if target != "missing.md" else None

    result = markdown_text(
        "<!channel> <@U12345678> <@U87654321> & <tag>\n"
        "[私|有](a&b.md) `notes.md` `missing.md` [web](https://example.com)\n"
        "```python\n<@U12345678> [x](secret.md) `secret.md`\n```",
        mentions=frozenset({"U12345678"}), resolve_link=resolve,
    )
    assert requested == ["a&b.md", "notes.md", "missing.md"]
    assert result.startswith("&lt;!channel&gt; <@U12345678> &lt;@U87654321&gt; &amp; &lt;tag&gt;")
    assert "<https://app.test/?file=a&amp;b.md&amp;view=1|私¦有>" in result
    assert "`missing.md`" in result
    assert "<https://example.com|web>" in result
    assert result.endswith("```\n&lt;@U12345678&gt; [x](secret.md) `secret.md`\n```")


def test_resolver_failure_propagates_without_emitting_a_partial_reply():
    def denied(_target):
        raise PermissionError("link is unavailable")

    with pytest.raises(PermissionError, match="unavailable"):
        markdown_text("[private](private.md)", resolve_link=denied)


def test_resolver_receives_original_entity_literals_once():
    requested = []
    markdown_text("[link](a&amp;b.md) `c&lt;d.md`", resolve_link=requested.append)
    assert requested == ["a&amp;b.md", "c&lt;d.md"]


def test_direct_web_links_are_bounded_and_escape_labels():
    assert markdown_text("[long](https://example.com/" + "x" * 1600 + ")") == "long"
    assert markdown_text("[a|b](https://example.com/?a=1&b=2)") == (
        "<https://example.com/?a=1&amp;b=2|a¦b>"
    )


@pytest.mark.parametrize("limit", [30, 2600, 2900, 2992])
def test_shared_chunker_keeps_fitting_links_intact_and_unicode_unmodified(limit):
    link = "<https://example.com|🦀>"
    text = "🌊" * (limit - 5) + link + "文档" * 40
    chunks = split_text(text, limit=limit)
    assert "".join(chunks) == text
    assert any(link in chunk for chunk in chunks)
    assert all(len(chunk) <= limit for chunk in chunks)
    assert [block["text"]["text"] for block in text_blocks(text, limit=limit)] == chunks


def test_split_options_keep_legacy_newline_policy_explicit():
    text = "abcdefghij\n\nklmnopqrst"
    assert "".join(split_text(text, limit=12)) == text
    trimmed = split_text(text, limit=12, trim_boundary_newlines=True)
    assert "".join(trimmed) == "abcdefghij\nklmnopqrst"
    assert split_text("") == []
    with pytest.raises(ValueError, match="between 3 and 2992"):
        split_text("text", limit=0)
