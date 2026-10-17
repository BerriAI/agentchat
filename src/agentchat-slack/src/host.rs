//! Slack protocol helpers for applications that own authentication and durable ingress.
use crate::{events, policy, route::SlackTimestamp};
use agentchat_core::{Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;

#[derive(Clone, Debug)]
pub struct SlackHost {
    pub bot_user_id: String,
    pub workspace_id: Option<String>,
    pub exclude_prefixes: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct SlackRouting {
    pub text: String,
    pub mentioned: bool,
    pub excluded: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ThreadOrigin {
    pub team_id: String,
    pub channel_id: String,
    pub thread_ts: String,
    pub message_ts: String,
}

/// Projected current-message data bound to the authenticated event's provenance.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CurrentReference {
    #[serde(flatten)]
    pub origin: ThreadOrigin,
    pub user_id: String,
    pub data: Value,
}

#[derive(Clone, Debug, Serialize)]
pub struct SlackHostEvent {
    #[serde(flatten)]
    pub origin: ThreadOrigin,
    pub user_id: String,
    pub text: String,
    pub mentioned: bool,
    pub direct: bool,
    pub excluded: bool,
    pub files: Vec<agentchat_core::Attachment>,
    pub reference: CurrentReference,
}

fn identifier(value: &str, prefixes: &str) -> bool {
    (8..=31).contains(&value.len())
        && value.starts_with(|c| prefixes.contains(c))
        && value
            .bytes()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
}
fn timestamp(value: &str) -> Option<SlackTimestamp> {
    let (seconds, fraction) = value.split_once('.')?;
    ((10..=16).contains(&seconds.len()) && (1..=9).contains(&fraction.len()))
        .then(|| SlackTimestamp::parse(value))
        .flatten()
}
impl SlackHost {
    pub fn routing_text(&self, text: &str) -> SlackRouting {
        let routed = events::routing_text(text, Some(&self.bot_user_id));
        let lower = routed.text.to_lowercase();
        let excluded = self.exclude_prefixes.iter().any(|prefix| {
            lower
                .strip_prefix(&prefix.to_lowercase())
                .is_some_and(|rest| rest.is_empty() || rest.starts_with(char::is_whitespace))
        });
        SlackRouting {
            text: routed.text,
            mentioned: routed.mentioned,
            excluded,
        }
    }

    pub fn normalize_event(&self, payload: &Value) -> Result<Option<SlackHostEvent>> {
        let Some(event) = payload.get("event").and_then(Value::as_object) else {
            return Ok(None);
        };
        if !matches!(
            event.get("type").and_then(Value::as_str),
            Some("message" | "app_mention")
        ) {
            return Ok(None);
        }
        let invalid = || Error::InvalidInput("Invalid Slack event fields".into());
        if ["subtype", "channel_type"].iter().any(|key| {
            event
                .get(*key)
                .is_some_and(|value| !value.is_null() && !value.is_string())
        }) {
            return Err(invalid());
        }
        if event
            .get("subtype")
            .is_some_and(|value| !value.is_null() && value != "file_share")
        {
            return Ok(None);
        }
        if event.get("bot_id").is_some_and(|value| !value.is_null())
            || event
                .get("bot_profile")
                .is_some_and(|value| !value.is_null())
            || event.get("user").and_then(Value::as_str) == Some(&self.bot_user_id)
        {
            return Ok(None);
        }
        let text = event
            .get("text")
            .map_or(Some(""), Value::as_str)
            .ok_or_else(invalid)?;
        let user = event
            .get("user")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        let origin = ThreadOrigin {
            team_id: payload["team_id"].as_str().ok_or_else(invalid)?.into(),
            channel_id: event
                .get("channel")
                .and_then(Value::as_str)
                .ok_or_else(invalid)?
                .into(),
            message_ts: event
                .get("ts")
                .and_then(Value::as_str)
                .ok_or_else(invalid)?
                .into(),
            thread_ts: event
                .get("thread_ts")
                .filter(|v| !v.is_null())
                .or_else(|| event.get("ts"))
                .and_then(Value::as_str)
                .ok_or_else(invalid)?
                .into(),
        };
        self.validate_origin(&origin)?;
        if !identifier(user, "UW") {
            return Err(invalid());
        }
        let routing = self.routing_text(text);
        if routing.excluded {
            return Ok(None);
        }
        let mut input = payload.clone();
        if input["event"]["channel_type"].is_null() && !origin.channel_id.starts_with('D') {
            input["event"]["channel_type"] = json!("channel");
        }
        let Some(message) = events::normalize_scoped(
            &input,
            Some(&self.bot_user_id),
            self.workspace_id.as_deref(),
            "slack",
        ) else {
            return Ok(None);
        };
        Ok(Some(SlackHostEvent {
            direct: origin.channel_id.starts_with('D') && payload["event"]["channel_type"] == "im",
            mentioned: event["type"] == "app_mention" || routing.mentioned,
            user_id: user.into(),
            text: message.text,
            excluded: routing.excluded,
            files: message.attachments,
            reference: CurrentReference {
                origin: origin.clone(),
                user_id: user.into(),
                data: message
                    .metadata
                    .get("slack_reference")
                    .cloned()
                    .unwrap_or_else(|| json!({})),
            },
            origin,
        }))
    }

    fn validate_origin(&self, origin: &ThreadOrigin) -> Result<()> {
        let invalid = || Error::InvalidInput("Invalid Slack thread origin or workspace".into());
        if !identifier(&origin.team_id, "T")
            || !identifier(&origin.channel_id, "CDG")
            || self
                .workspace_id
                .as_ref()
                .is_some_and(|team| team != &origin.team_id)
        {
            return Err(invalid());
        }
        policy::ReplyDestination::validate(&origin.channel_id, Some(&origin.thread_ts))?;
        let root = timestamp(&origin.thread_ts).ok_or_else(invalid)?;
        let current = timestamp(&origin.message_ts).ok_or_else(invalid)?;
        if current.is_before(&root) {
            return Err(invalid());
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ContextLimits {
    pub max_pages: usize,
    pub max_chars: usize,
    pub max_message_chars: usize,
}
impl ContextLimits {
    pub fn validate(self) -> Result<Self> {
        if self.max_pages == 0
            || self.max_pages > 100
            || self.max_chars == 0
            || self.max_chars > 1_000_000
            || self.max_message_chars == 0
            || self.max_message_chars > 1_000_000
        {
            return Err(Error::InvalidInput("Invalid Slack context limits".into()));
        }
        Ok(self)
    }
}
#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CoverageStatus {
    Complete,
    Partial,
    Unavailable,
}
#[derive(Clone, Debug, Serialize)]
pub struct ContextMessage {
    pub ts: String,
    pub author: String,
    pub content: String,
    pub truncated: bool,
}
#[derive(Clone, Debug, Serialize)]
pub struct ThreadContext {
    #[serde(flatten)]
    pub origin: ThreadOrigin,
    pub status: CoverageStatus,
    pub messages: Vec<ContextMessage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub warning: Option<String>,
}

/// Stateful page policy reused by host runtimes; contains no credentials or I/O.
pub struct ThreadContextReader {
    host: SlackHost,
    context: ThreadContext,
    limits: ContextLimits,
    root: SlackTimestamp,
    current: SlackTimestamp,
    cursor: String,
    cursors: HashSet<String>,
    seen: HashSet<String>,
    pages: usize,
    current_reference: Value,
    current_author: Option<String>,
}
impl ThreadContextReader {
    pub fn new(host: SlackHost, origin: ThreadOrigin, limits: ContextLimits) -> Result<Self> {
        host.validate_origin(&origin)?;
        let root = timestamp(&origin.thread_ts).expect("validated root");
        let current = timestamp(&origin.message_ts).expect("validated message");
        Ok(Self {
            host,
            limits: limits.validate()?,
            root,
            current,
            cursor: String::new(),
            cursors: HashSet::new(),
            seen: HashSet::new(),
            pages: 0,
            current_reference: json!({}),
            current_author: None,
            context: ThreadContext {
                origin,
                status: CoverageStatus::Complete,
                messages: Vec::new(),
                error: None,
                warning: None,
            },
        })
    }
    pub fn seed(&mut self, reference: CurrentReference, user_id: &str) -> Result<()> {
        if reference.origin != self.context.origin
            || reference.user_id != user_id
            || !identifier(user_id, "UW")
        {
            return Err(Error::InvalidInput(
                "Slack reference belongs to a different event".into(),
            ));
        }
        self.current_author = Some(user_id.into());
        let source = self.merge_current(events::reference(&reference.data, true));
        let ts = self.current.as_str().to_owned();
        self.store(&ts, user_id, source);
        Ok(())
    }
    fn merge_current(&mut self, source: Value) -> Value {
        for (key, incoming) in source.as_object().expect("projected reference") {
            if let (Some(Value::Array(existing)), Value::Array(items)) =
                (self.current_reference.get_mut(key), incoming)
            {
                for item in items {
                    if !existing.contains(item) {
                        existing.push(item.clone());
                    }
                }
            } else {
                self.current_reference[key] = incoming.clone();
            }
        }
        self.current_reference.clone()
    }
    pub fn request(&self) -> Value {
        json!({"channel":self.context.origin.channel_id,"ts":self.context.origin.thread_ts,
            "latest":self.context.origin.message_ts,"inclusive":true,"limit":100,
            "cursor":self.cursor,"include_all_metadata":true})
    }
    pub fn fail(&mut self, code: &str) {
        self.context.status = if self.context.messages.is_empty() {
            CoverageStatus::Unavailable
        } else {
            CoverageStatus::Partial
        };
        self.context.error = Some(code.chars().take(100).collect());
    }
    fn partial(&mut self, reason: &str) {
        self.context.status = CoverageStatus::Partial;
        self.context.warning = Some(reason.into());
    }
    fn include(&mut self, record: &Value) {
        let Some(input) = events::history_record(record, Some(&self.host.bot_user_id)) else {
            return;
        };
        let ts = &input.timestamp;
        if ts.is_before(&self.root)
            || self.current.is_before(ts)
            || record
                .get("thread_ts")
                .is_some_and(|thread| thread != self.root.as_str())
            || self.host.routing_text(&input.text).excluded
            || !self.seen.insert(ts.as_str().into())
        {
            return;
        }
        let source = if ts.as_str() == self.current.as_str() {
            if self
                .current_author
                .as_ref()
                .is_some_and(|author| author != &input.sender.id)
            {
                self.partial("current message author mismatch");
                return;
            }
            self.merge_current(events::reference(record, true))
        } else {
            input.reference
        };
        self.store(ts.as_str(), &input.sender.id, source);
    }
    fn store(&mut self, ts: &str, author: &str, source: Value) {
        if source.as_object().is_none_or(|v| v.is_empty()) {
            return;
        }
        let serialized = source.to_string();
        let limit = self.limits.max_message_chars.min(self.limits.max_chars);
        let content: String = serialized.chars().take(limit).collect();
        let truncated = serialized.chars().count() > limit;
        if truncated {
            self.partial("message limit reached");
        }
        self.context.messages.retain(|message| message.ts != ts);
        self.context.messages.push(ContextMessage {
            ts: ts.into(),
            author: author.into(),
            content,
            truncated,
        });
        self.context.messages.sort_by(|a, b| {
            let a = SlackTimestamp::parse(&a.ts).expect("validated timestamp");
            let b = SlackTimestamp::parse(&b.ts).expect("validated timestamp");
            if a.is_before(&b) {
                std::cmp::Ordering::Less
            } else if b.is_before(&a) {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        });
        while self
            .context
            .messages
            .iter()
            .map(|m| m.content.chars().count())
            .sum::<usize>()
            > self.limits.max_chars
        {
            let index = usize::from(self.context.messages[0].ts == self.root.as_str());
            self.context.messages.remove(index);
            self.partial("context limit reached");
        }
    }
    /// Returns true when fetching should stop; incomplete reads remain explicit.
    pub fn accept(&mut self, page: &Value) -> bool {
        if page["ok"] != true {
            self.fail(page["error"].as_str().unwrap_or("invalid_response"));
            return true;
        }
        let Some(records) = page["messages"].as_array() else {
            self.fail("invalid_response");
            return true;
        };
        for record in records {
            self.include(record);
        }
        self.pages += 1;
        if page["has_more"] == false {
            return true;
        }
        if page["has_more"] != true {
            self.partial("pagination coverage unavailable");
            return true;
        }
        let next = page["response_metadata"]["next_cursor"]
            .as_str()
            .or_else(|| page["next_arguments"]["cursor"].as_str())
            .unwrap_or("");
        if next.is_empty() || !self.cursors.insert(next.into()) {
            self.partial("invalid pagination cursor");
            return true;
        }
        if self.pages >= self.limits.max_pages {
            self.partial("page limit reached");
            return true;
        }
        self.cursor = next.into();
        false
    }
    pub fn finish(mut self) -> ThreadContext {
        if self.context.status == CoverageStatus::Complete
            && !self.seen.contains(self.root.as_str())
        {
            self.partial("The thread root was not returned.");
        }
        self.context
    }
}
