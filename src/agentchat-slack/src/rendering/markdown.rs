//! Explicit Markdown-to-mrkdwn conversion. Plain RichReply text is never rewritten.

use regex::Regex;
use std::sync::LazyLock;

static FENCES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"```[\s\S]*?(?:```|\z)").unwrap());
static CODE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"```[\s\S]*?(?:```|\z)|`[^`\n]+`").unwrap());
// Python-compatible whitespace also includes the ASCII separator controls.
static HEADINGS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?m)^#{1,6}[ \t]+([^\s\x1c-\x1f][^\r\n]*)").unwrap());
static FENCE_INFO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A```[^`\r\n]*\r?\n").unwrap());
static LINKS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"!?\[([^\]\n]+)\]\(([^\s\x1c-\x1f)<>]+)\)").unwrap());
static MENTIONS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"&lt;@([UW][A-Z0-9]{7,30})&gt;").unwrap());

#[derive(Default)]
pub struct MarkdownOptions<'a> {
    pub allowed_mentions: &'a [String],
}

enum Span {
    Text(String),
    InlineCode(String),
    FencedCode(String),
}

impl Span {
    fn content(&self) -> &str {
        match self {
            Self::Text(value) | Self::InlineCode(value) | Self::FencedCode(value) => value,
        }
    }
}

fn spans(text: &str, pattern: &Regex) -> Vec<Span> {
    let mut output = Vec::new();
    let mut cursor = 0;
    for found in pattern.find_iter(text) {
        output.push(Span::Text(text[cursor..found.start()].into()));
        let code = found.as_str().to_owned();
        output.push(if code.starts_with("```") {
            Span::FencedCode(code)
        } else {
            Span::InlineCode(code)
        });
        cursor = found.end();
    }
    output.push(Span::Text(text[cursor..].into()));
    output
}

fn joined(spans: &[Span]) -> String {
    spans.iter().map(Span::content).collect()
}

fn paired_bold(spans: &mut [Span], replacement: &str) {
    let markers: Vec<_> = spans
        .iter()
        .enumerate()
        .flat_map(|(index, span)| match span {
            Span::Text(text) => text
                .match_indices("**")
                .map(move |(offset, _)| (index, offset))
                .collect(),
            _ => Vec::new(),
        })
        .collect();
    // Preserve the unmatched final marker, including globs and exponent notation.
    for &(index, offset) in markers[..markers.len() / 2 * 2].iter().rev() {
        if let Span::Text(text) = &mut spans[index] {
            text.replace_range(offset..offset + 2, replacement);
        }
    }
}

fn blocks(text: &str) -> String {
    let mut pieces = spans(text, &FENCES);
    for span in &mut pieces {
        match span {
            Span::FencedCode(code) => *code = FENCE_INFO.replace(code, "```\n").into_owned(),
            Span::Text(text) => {
                *text = HEADINGS
                    .replace_all(text, |found: &regex::Captures<'_>| {
                        let mut heading =
                            spans(super::super::events::trim_python(&found[1]), &CODE);
                        paired_bold(&mut heading, "");
                        format!("*{}*", joined(&heading))
                    })
                    .into_owned();
            }
            Span::InlineCode(_) => unreachable!("the block tokenizer only matches fences"),
        }
    }
    joined(&pieces)
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn unescape(text: &str) -> String {
    // Decode exactly the escaping above, once; preserve original entity literals.
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
}

fn links<E>(
    text: &str,
    resolve_link: &mut impl FnMut(&str) -> Result<Option<String>, E>,
) -> Result<String, E> {
    let mut result = String::new();
    let mut cursor = 0;
    for found in LINKS.captures_iter(text) {
        let whole = found.get(0).unwrap();
        result.push_str(&text[cursor..whole.start()]);
        let label = &found[1];
        let target = &found[2];
        let url = if target.starts_with("https://") || target.starts_with("http://") {
            Some(target.to_owned())
        } else {
            resolve_link(&unescape(target))?
                .filter(|url| !url.is_empty())
                .map(|url| escape(&url))
        };
        match url {
            Some(url) if url.chars().count() > 1500 => result.push_str(label),
            Some(url) => result.push_str(&format!("<{}|{}>", url, label.replace('|', "¦"))),
            None => result.push_str(whole.as_str()),
        }
        cursor = whole.end();
    }
    result.push_str(&text[cursor..]);
    Ok(result)
}

/// Render Markdown, optionally resolving application-owned local references.
/// The resolver sees unescaped targets; it is never called for fenced code or HTTP Markdown links.
pub fn markdown_text<E>(
    text: &str,
    options: MarkdownOptions<'_>,
    mut resolve_link: impl FnMut(&str) -> Result<Option<String>, E>,
) -> Result<String, E> {
    let mut pieces = spans(&blocks(&escape(text)), &CODE);
    for span in &mut pieces {
        if let Span::Text(text) = span {
            let linked = links(text, &mut resolve_link)?;
            *text = MENTIONS
                .replace_all(&linked, |found: &regex::Captures<'_>| {
                    if options.allowed_mentions.iter().any(|id| id == &found[1]) {
                        format!("<@{}>", &found[1])
                    } else {
                        found[0].to_owned()
                    }
                })
                .into_owned();
        }
    }
    paired_bold(&mut pieces, "*");
    for span in &mut pieces {
        if let Span::InlineCode(code) = span {
            let label = &code[1..code.len() - 1];
            if let Some(url) = resolve_link(&unescape(label))?.filter(|url| !url.is_empty()) {
                *code = format!("<{}|{}>", escape(&url), label.replace('|', "¦"));
            }
        }
    }
    Ok(joined(&pieces))
}
