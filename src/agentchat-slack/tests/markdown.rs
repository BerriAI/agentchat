use agentchat_slack::rendering::{
    markdown_text, split_text, text_blocks, MarkdownOptions, SplitOptions,
};
use std::convert::Infallible;

fn render(text: &str) -> String {
    markdown_text(text, MarkdownOptions::default(), |_| {
        Ok::<_, Infallible>(None)
    })
    .unwrap()
}

#[test]
fn headings_and_paired_bold_leave_inline_code_and_dangling_markers_literal() {
    for (input, expected) in [
        (
            "### **Model** `name` is **available**",
            "*Model `name` is available*",
        ),
        (
            "#### `**literal**` and **bold**",
            "*`**literal**` and bold*",
        ),
        ("**see `x` now** **", "*see `x` now* **"),
        ("# Glob: **/*.py", "*Glob: **/*.py*"),
        ("# Math: 2**10", "*Math: 2**10*"),
        ("###   \nNormal", "###   \nNormal"),
        (
            "# [Docs](https://example.com)",
            "*<https://example.com|Docs>*",
        ),
    ] {
        assert_eq!(render(input), expected);
    }
}

#[test]
fn fences_drop_info_strings_and_protect_closed_and_unclosed_code() {
    for closing in ["\n```", ""] {
        let code = "# **literal** <@U12345678> [report](report.md)\n`inline`";
        assert_eq!(
            render(&format!("```json\r\n{code}{closing}")),
            format!(
                "```\n{}{closing}",
                code.replace('<', "&lt;").replace('>', "&gt;")
            )
        );
    }
    assert_eq!(
        render("```same line``` and `python`"),
        "```same line``` and `python`"
    );
}

#[test]
fn only_allowed_mentions_activate_and_local_links_use_the_host_resolver() {
    let mentions = vec!["U12345678".into()];
    let mut requested = Vec::new();
    let text = markdown_text(
        "<@U12345678> <@U87654321> <!here> & <tag>\n[私|有](a&b.md) `notes.md` `missing.md`\n```\n[code](secret.md)\n```",
        MarkdownOptions { allowed_mentions: &mentions },
        |target| {
            requested.push(target.to_owned());
            Ok::<_, Infallible>((target != "missing.md").then(|| format!("https://app.test/?file={target}&view=1")))
        },
    ).unwrap();
    assert_eq!(requested, ["a&b.md", "notes.md", "missing.md"]);
    assert!(text.contains("<@U12345678> &lt;@U87654321&gt; &lt;!here&gt; &amp; &lt;tag&gt;"));
    assert!(text.contains("<https://app.test/?file=a&amp;b.md&amp;view=1|私¦有>"));
    assert!(text.contains("`missing.md`"));
    assert!(text.contains("[code](secret.md)"));
}

#[test]
fn chunks_protect_unicode_links_and_fences_with_explicit_newline_policy() {
    let link = "<https://example.test/🦀|文档>";
    let text = format!("{}{}{}", "🌊".repeat(2980), link, "z".repeat(40));
    let blocks = text_blocks(&text).unwrap();
    let bodies: Vec<_> = blocks
        .iter()
        .map(|block| block["text"]["text"].as_str().unwrap())
        .collect();
    assert_eq!(bodies.concat(), text);
    assert!(bodies.iter().any(|body| body.contains(link)));
    for trim_boundary_newlines in [false, true] {
        let text = "abcdefghij\n\nklmnopqrst";
        let chunks = split_text(
            text,
            SplitOptions {
                limit: 12,
                trim_boundary_newlines,
            },
        )
        .unwrap();
        assert_eq!(
            chunks.concat(),
            if trim_boundary_newlines {
                "abcdefghij\nklmnopqrst"
            } else {
                text
            }
        );
    }
    let text = format!("{}{}", "a".repeat(11), "```\n🦀🦀🦀🦀🦀\n```");
    let chunks = split_text(
        &text,
        SplitOptions {
            limit: 12,
            trim_boundary_newlines: false,
        },
    )
    .unwrap();
    assert!(chunks
        .iter()
        .all(|chunk| chunk.matches("```").count() % 2 == 0));
    assert!(split_text(
        "x",
        SplitOptions {
            limit: 0,
            ..SplitOptions::default()
        }
    )
    .is_err());
}
