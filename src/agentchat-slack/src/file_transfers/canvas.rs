//! Inert, bounded text projection of Slack's generated Canvas HTML export.
use html5gum::{DefaultEmitter, StartTag, Token, Tokenizer};
use regex::Regex;
use serde_json::Value;
use std::sync::LazyLock;

pub const MIME: &str = "application/vnd.slack-docs";
pub const PREVIEW_CHARACTERS: usize = 8000;
pub const PREVIEW_SOURCE_BYTES: usize = 256 * 1024;
pub const TRUNCATION_NOTICE:&str="\n\n[Canvas text preview truncated. The complete original HTML is retained in the attachment.]";
const MAX_NESTING: usize = 256;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanvasError {
    InvalidUtf8,
    InvalidExport,
    Nesting,
    UnclosedWrapper,
}
impl std::fmt::Display for CanvasError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::InvalidUtf8 => "The Canvas export was not valid UTF-8.",
            Self::InvalidExport => "The response was not a readable Canvas export.",
            Self::Nesting => "The Canvas export exceeded the HTML nesting limit.",
            Self::UnclosedWrapper => "The Canvas export did not finish its wrapper.",
        })
    }
}
pub fn is_canvas(file: &Value) -> bool {
    file["mimetype"] == MIME && file["filetype"] == "quip" && file["mode"] == "quip"
}
pub fn bounded_preview(text: &str, truncated: bool) -> String {
    if truncated || text.chars().count() > PREVIEW_CHARACTERS {
        format!(
            "{}{}",
            text.chars()
                .take(PREVIEW_CHARACTERS - TRUNCATION_NOTICE.chars().count())
                .collect::<String>()
                .trim_end(),
            TRUNCATION_NOTICE
        )
    } else {
        text.into()
    }
}
struct Frame {
    tag: String,
    hidden: bool,
    checklist: bool,
}
struct List {
    ordered: bool,
    number: usize,
    checklist: bool,
}
#[derive(Default)]
struct Preview {
    frames: Vec<Frame>,
    lists: Vec<List>,
    items: Vec<(Option<usize>, String)>,
    parts: Vec<String>,
    root_depth: Option<usize>,
    root_closed: bool,
    invalid: bool,
    truncated: bool,
    length: usize,
}
impl Preview {
    fn append(&mut self, text: &str) -> Option<usize> {
        let remaining = (PREVIEW_CHARACTERS + 1).saturating_sub(self.length);
        if text.chars().count() > remaining {
            self.truncated = true;
        }
        if remaining == 0 {
            return None;
        }
        let text: String = text.chars().take(remaining).collect();
        self.length += text.chars().count();
        self.parts.push(text);
        Some(self.parts.len() - 1)
    }
    fn start(&mut self, start: StartTag) -> Result<(), CanvasError> {
        static HIDDEN: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)(?:display\s*:\s*none|visibility\s*:\s*hidden)").expect("static regex")
        });
        let tag = String::from_utf8_lossy(&start.name).into_owned();
        let attr = |key: &str| {
            start
                .attributes
                .get(key.as_bytes())
                .map(|v| String::from_utf8_lossy(v).into_owned())
        };
        let classes = attr("class").unwrap_or_default();
        let class = |v: &str| classes.split_whitespace().any(|x| x == v);
        let hidden = self.frames.last().is_some_and(|v| v.hidden)
            || matches!(
                tag.as_str(),
                "head"
                    | "script"
                    | "style"
                    | "noscript"
                    | "template"
                    | "iframe"
                    | "object"
                    | "svg"
                    | "math"
                    | "canvas"
                    | "form"
            )
            || attr("hidden").is_some()
            || attr("aria-hidden").as_deref() == Some("true")
            || HIDDEN.is_match(attr("style").as_deref().unwrap_or_default());
        let checklist = self.frames.last().is_some_and(|v| v.checklist)
            || attr("data-section-style").as_deref() == Some("7")
            || class("checklist");
        if tag == "div" && class("quip-canvas-content") {
            if self.root_depth.is_some() {
                self.invalid = true;
            } else {
                self.root_depth = Some(self.frames.len() + 1);
            }
        }
        let inside = self
            .root_depth
            .is_some_and(|d| !self.root_closed && self.frames.len() + 1 >= d);
        if !matches!(
            tag.as_str(),
            "area"
                | "base"
                | "br"
                | "col"
                | "embed"
                | "hr"
                | "img"
                | "input"
                | "link"
                | "meta"
                | "param"
                | "source"
                | "track"
                | "wbr"
        ) {
            if self.frames.len() >= MAX_NESTING {
                return Err(CanvasError::Nesting);
            }
            self.frames.push(Frame {
                tag: tag.clone(),
                hidden,
                checklist,
            });
        }
        if inside
            && (tag == "form"
                || (tag == "input"
                    && attr("type").is_some_and(|s| s.eq_ignore_ascii_case("password"))))
        {
            self.invalid = true;
        }
        if !inside || hidden {
            return Ok(());
        }
        if let Some(level) = heading(&tag) {
            self.append(&format!("\n\n{} ", "#".repeat(level)));
        } else {
            match tag.as_str() {
                "p" | "div" | "section" | "article" | "blockquote" | "tr" | "br" | "hr" => {
                    self.append("\n");
                }
                "ul" | "ol" => {
                    self.append("\n");
                    self.lists.push(List {
                        ordered: tag == "ol",
                        number: 0,
                        checklist,
                    });
                }
                "li" => {
                    self.append("\n");
                    let indent = "  ".repeat(self.lists.len().saturating_sub(1).min(8));
                    let (base, list_task) = if let Some(list) = self.lists.last_mut() {
                        list.number += 1;
                        (
                            format!(
                                "{indent}{}",
                                if list.ordered {
                                    format!("{}. ", list.number)
                                } else {
                                    "- ".into()
                                }
                            ),
                            list.checklist,
                        )
                    } else {
                        (format!("{indent}- "), false)
                    };
                    let state = attr("data-checked").or_else(|| attr("aria-checked"));
                    let task = list_task
                        || class("checked")
                        || matches!(state.as_deref(), Some("true" | "false"));
                    let marker = if class("checked") || state.as_deref() == Some("true") {
                        "[x] "
                    } else {
                        "[ ] "
                    };
                    let index = self.append(&format!("{base}{}", if task { marker } else { "" }));
                    self.items.push((index, base));
                }
                "input" if attr("type").is_some_and(|s| s.eq_ignore_ascii_case("checkbox")) => {
                    let marker = if attr("checked").is_some()
                        || attr("aria-checked").as_deref() == Some("true")
                    {
                        "[x] "
                    } else {
                        "[ ] "
                    };
                    if let Some((Some(index), base)) = self.items.last() {
                        let replacement = format!("{base}{marker}");
                        self.length = self
                            .length
                            .saturating_sub(self.parts[*index].chars().count())
                            + replacement.chars().count();
                        self.parts[*index] = replacement;
                    } else {
                        self.append(marker);
                    }
                }
                "td" | "th" => {
                    self.append(" | ");
                }
                _ => {}
            }
        }
        Ok(())
    }
    fn end(&mut self, tag: &str) {
        let Some(index) = self.frames.iter().rposition(|frame| frame.tag == tag) else {
            return;
        };
        if self
            .root_depth
            .is_some_and(|d| !self.root_closed && index + 1 >= d)
            && !self.frames[index].hidden
        {
            if tag == "li" {
                self.items.pop();
            }
            if matches!(tag, "ul" | "ol") {
                self.lists.pop();
            }
            if matches!(
                tag,
                "li" | "p" | "div" | "ul" | "ol" | "tr" | "section" | "article" | "blockquote"
            ) || heading(tag).is_some()
            {
                self.append("\n");
            }
        }
        if self.root_depth == Some(index + 1) {
            self.root_closed = true;
        }
        self.frames.truncate(index);
    }
    fn data(&mut self, text: &str) {
        if self
            .root_depth
            .is_none_or(|d| self.root_closed || self.frames.len() < d)
            || self.frames.last().is_none_or(|f| f.hidden)
        {
            return;
        }
        static SPACE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").expect("static regex"));
        self.append(&SPACE.replace_all(text, " "));
    }
}
fn heading(tag: &str) -> Option<usize> {
    let bytes = tag.as_bytes();
    (bytes.len() == 2 && bytes[0] == b'h' && (b'1'..=b'6').contains(&bytes[1]))
        .then(|| (bytes[1] - b'0') as usize)
}
/// `prefix` is bounded by PREVIEW_SOURCE_BYTES; tail is the final <=256 bytes.
/// The caller retains the original file separately and resets its seek position.
pub fn preview(prefix: &[u8], tail: &[u8], truncated_source: bool) -> Result<String, CanvasError> {
    let prefix = prefix.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(prefix);
    let text = match std::str::from_utf8(prefix) {
        Ok(v) => v,
        Err(e) if truncated_source && e.error_len().is_none() => {
            std::str::from_utf8(&prefix[..e.valid_up_to()]).map_err(|_| CanvasError::InvalidUtf8)?
        }
        Err(_) => return Err(CanvasError::InvalidUtf8),
    };
    let mut emitter = DefaultEmitter::default();
    emitter.naively_switch_states(true);
    let mut parser = Preview::default();
    for token in Tokenizer::new_with_emitter(text, emitter) {
        match token.expect("in-memory tokenizer cannot fail") {
            Token::StartTag(tag) => parser.start(tag)?,
            Token::EndTag(tag) => parser.end(&String::from_utf8_lossy(&tag.name)),
            Token::String(text) => parser.data(&String::from_utf8_lossy(&text)),
            _ => {}
        }
    }
    static TRAILING: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[ \t]+\n").expect("static regex"));
    static NEWLINES: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\n{3,}").expect("static regex"));
    let joined = parser.parts.concat();
    let text = NEWLINES
        .replace_all(&TRAILING.replace_all(&joined, "\n"), "\n\n")
        .trim()
        .to_owned();
    if parser.invalid || parser.root_depth.is_none() || text.is_empty() {
        return Err(CanvasError::InvalidExport);
    }
    if truncated_source {
        if !String::from_utf8_lossy(tail)
            .trim_end()
            .to_ascii_lowercase()
            .ends_with("</div>")
        {
            return Err(CanvasError::UnclosedWrapper);
        }
    } else if !parser.root_closed {
        return Err(CanvasError::UnclosedWrapper);
    }
    Ok(bounded_preview(&text, truncated_source || parser.truncated))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inert_canvas_text() {
        let html=br#"<div class="quip-canvas-content"><h1>Plan &amp; notes</h1><p>Visible</p><script>secret</script><p hidden>hidden</p><ul class="checklist"><li><input type="checkbox" checked>ship</li><li>next</li></ul></div>"#;
        assert_eq!(
            preview(html, b"", false).unwrap(),
            "# Plan & notes\n\nVisible\n\n- [x] ship\n\n- [ ] next"
        );
    }
    #[test]
    fn rejects_auth_and_truncation() {
        assert!(preview(b"<html>Sign in</html>", b"", false).is_err());
        assert!(preview(b"<div class='quip-canvas-content'>text", b"", false).is_err());
        assert!(preview(
            b"<div class='quip-canvas-content'><form>login</form></div>",
            b"",
            false
        )
        .is_err());
    }
}
