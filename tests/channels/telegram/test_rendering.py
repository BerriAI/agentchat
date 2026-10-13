import random

import pytest

from agentchat.channels.telegram.rendering import render, text_payload


def _units(text):
    return text.encode("utf-16-le")


def _slice(text, entity):
    start = entity["offset"] * 2
    return _units(text)[start:start + entity["length"] * 2].decode("utf-16-le")


def _only(entities, entity_type):
    found = [e for e in entities if e["type"] == entity_type]
    assert len(found) == 1, entities
    return found[0]


def _types(markdown):
    return [e["type"] for e in render(markdown)[1]]


def test_rendering_fenced_block_becomes_pre_with_language():
    text, entities = render("intro\n\n```ts\nconst a = 1;\n```")
    pre = _only(entities, "pre")
    assert pre["language"] == "ts"
    assert _slice(text, pre) == "const a = 1;"


def test_rendering_backticks_inside_a_fenced_block_survive():
    text, entities = render("```sh\necho `date`\n```")
    assert text == "echo `date`"
    assert [e["type"] for e in entities] == ["pre"]


def test_rendering_nested_emphasis_produces_both_entities():
    assert set(_types("**bold with `code` inside**")) == {"bold", "code"}


def test_rendering_cjk_offsets_line_up():
    text, entities = render("日本語の `foo` テスト")
    assert _slice(text, _only(entities, "code")) == "foo"


def test_rendering_links_render_as_text_link():
    text, entities = render("see [docs](https://example.com) here")
    link = _only(entities, "text_link")
    assert link["url"] == "https://example.com"
    assert _slice(text, link) == "docs"


def test_rendering_emoji_before_and_inside_entities():
    text, entities = render("😀 **bold 😀 x** and `日本😀`")
    bold, code = _only(entities, "bold"), _only(entities, "code")
    assert (bold["offset"], bold["length"]) == (3, 9)
    assert _slice(text, bold) == "bold 😀 x"
    assert _slice(text, code) == "日本😀"
    assert code["length"] == 4


@pytest.mark.parametrize(
    "literal",
    [
        "snake_case_name", "a*b*c", "get_user_data()", "x * y * z", "file_name_v2.py", "2*3*4",
        "edit __init__.py", "run __main__", "a __b__ c", "[docs](./readme.md)",
    ],
)
def test_rendering_non_delimiters_stay_literal(literal):
    assert render(literal) == (literal, [])


def test_rendering_unclosed_fence_runs_to_end():
    text, entities = render("before\n\n~~~python\nx = 1\n\n**not bold**\n")
    pre = _only(entities, "pre")
    assert pre["language"] == "python"
    assert _slice(text, pre) == "x = 1\n\n**not bold**"
    assert text.startswith("before\n\n")


def test_rendering_fence_language_is_first_word_of_info_string():
    _, entities = render('```python title="x.py"\npass\n```')
    assert _only(entities, "pre")["language"] == "python"


def test_rendering_fence_without_language_has_no_language_key():
    _, entities = render("```\npass\n```")
    assert entities == [{"type": "pre", "offset": 0, "length": 4}]


def test_rendering_nested_bold_and_italic():
    text, entities = render("**bold _it_ end** and _**both**_")
    assert text == "bold it end and both"
    spans = [(e["type"], _slice(text, e)) for e in entities]
    assert spans == [
        ("bold", "bold it end"),
        ("italic", "it"),
        ("italic", "both"),
        ("bold", "both"),
    ]


def test_rendering_link_containing_bold():
    text, entities = render("[**big** docs](https://x.y/z)")
    assert text == "big docs"
    assert entities == [
        {"type": "text_link", "offset": 0, "length": 8, "url": "https://x.y/z"},
        {"type": "bold", "offset": 0, "length": 3},
    ]


def test_rendering_code_inside_link_drops_code_entity():
    text, entities = render("[`run()`](https://x.y)")
    assert text == "run()"
    assert [e["type"] for e in entities] == ["text_link"]


def test_rendering_code_containing_markers_stays_literal():
    text, entities = render("`**not bold** _x_ [a](https://b.c)`")
    assert text == "**not bold** _x_ [a](https://b.c)"
    assert [e["type"] for e in entities] == ["code"]


def test_rendering_crlf_input():
    text, entities = render("# Title\r\n\r\n- one\r\n- **two**\r\n\r\n```sh\r\nls\r\n```\r\n")
    assert "\r" not in text
    assert text == "Title\n\n• one\n• two\n\nls"
    assert [(e["type"], _slice(text, e)) for e in entities] == [
        ("bold", "Title"),
        ("bold", "two"),
        ("pre", "ls"),
    ]


def test_rendering_blocks_and_lists():
    text, entities = render("1. first\n2. second\n  * nested\n\n~~gone~~ ||secret||")
    assert text == "1. first\n2. second\n  • nested\n\ngone secret"
    assert [(e["type"], _slice(text, e)) for e in entities] == [
        ("strikethrough", "gone"),
        ("spoiler", "secret"),
    ]


def test_rendering_consecutive_quote_lines_form_one_blockquote():
    text, entities = render("> one\n> **two**\nafter")
    assert text == "one\ntwo\nafter"
    assert [(e["type"], _slice(text, e)) for e in entities] == [
        ("blockquote", "one\ntwo"),
        ("bold", "two"),
    ]


def test_rendering_backslash_escapes_punctuation_only():
    assert render(r"\*not italic\* and \_x\_") == ("*not italic* and _x_", [])
    assert render(r"C:\Users\name") == (r"C:\Users\name", [])


def test_text_payload_shape():
    assert text_payload("hi **you**") == {
        "text": "hi you",
        "entities": [{"type": "bold", "offset": 3, "length": 3}],
        "link_preview_options": {"is_disabled": True},
    }
    assert text_payload("plain") == {
        "text": "plain",
        "link_preview_options": {"is_disabled": True},
    }


@pytest.mark.parametrize("content", ["", "   \n\t", "```\n```", "```\n\n\n```", None])
def test_text_payload_rejects_empty(content):
    with pytest.raises(ValueError, match="Telegram replies require nonempty text"):
        text_payload(content)


def test_text_payload_limit_counts_utf16_units():
    assert text_payload("😀" * 2048)["text"] == "😀" * 2048
    with pytest.raises(ValueError, match="must fit 4,096 characters"):
        text_payload("😀" * 2049)


def test_text_payload_limit_applies_to_rendered_text():
    assert text_payload("**" + "x" * 4096 + "**")["entities"][0]["length"] == 4096
    with pytest.raises(ValueError, match="must fit 4,096 characters"):
        text_payload("x" * 4097)


_TOKENS = [
    "*", "**", "_", "__", "__init__.py", "~~", "||", "`", "```", "~~~", "\\", "\n", "\n\n", "\r\n",
    "# ", "- ", "1. ", "> ", "[", "](", ")", "](https://x.y)", "https://a.b", "py",
    "😀", "👨‍👩‍👧", "日本語", "é", "a", "word", "b c", " ", "\t", "get_user_data()",
]


def _is_code_point_boundary(units, index):
    if index * 2 >= len(units):
        return True
    unit = int.from_bytes(units[index * 2:index * 2 + 2], "little")
    return not 0xDC00 <= unit <= 0xDFFF


def test_rendering_random_markdown_never_breaks_entity_bounds():
    rng = random.Random(4096)
    for _ in range(2000):
        source = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 60)))
        text, entities = render(source)
        units = _units(text)
        total = len(units) // 2
        for e in entities:
            assert e["offset"] >= 0, (source, e)
            assert e["length"] > 0, (source, e)
            assert e["offset"] + e["length"] <= total, (source, e)
            assert _is_code_point_boundary(units, e["offset"]), (source, e)
            assert _is_code_point_boundary(units, e["offset"] + e["length"]), (source, e)
        assert entities == sorted(entities, key=lambda e: e["offset"])
        for n, outer in enumerate(entities):
            outer_end = outer["offset"] + outer["length"]
            for inner in entities[n + 1:]:
                if inner["offset"] >= outer_end:
                    continue
                assert inner["offset"] + inner["length"] <= outer_end, (source, outer, inner)
                assert outer["type"] not in ("code", "pre"), (source, outer, inner)
                assert not (outer["type"] == inner["type"] == "blockquote"), source
                if outer["type"] == "text_link":
                    assert inner["type"] not in ("code", "pre", "text_link"), source
