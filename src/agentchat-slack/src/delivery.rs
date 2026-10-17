//! One-attempt write receipts and bounded, read-only reconciliation.
//! Durable claims, credentials and application authorization stay in the host.
use crate::{
    message_request::{MessageMethod, MessageRequest},
    policy::ReplyDestination,
    route::SlackTimestamp,
};
use agentchat_core::{Error, Result, RichReply};
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::collections::HashSet;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryStatus {
    Confirmed,
    Rejected,
    Uncertain,
}

#[derive(Clone, Debug, Serialize)]
pub struct DeliveryResult {
    pub status: DeliveryStatus,
    pub receipt: Option<Value>,
    pub reason: Option<String>,
}
impl DeliveryResult {
    pub fn uncertain(reason: &str) -> Self {
        Self {
            status: DeliveryStatus::Uncertain,
            receipt: None,
            reason: Some(reason.into()),
        }
    }
    pub fn from_response(request: &MessageRequest, response: Value) -> Self {
        if response.get("ok") == Some(&Value::Bool(false)) {
            return Self {
                status: DeliveryStatus::Rejected,
                receipt: Some(response),
                reason: None,
            };
        }
        let timestamp = response
            .get("ts")
            .and_then(Value::as_str)
            .and_then(SlackTimestamp::parse);
        let channel_matches = response
            .get("channel")
            .is_none_or(|channel| channel == &request.payload["channel"]);
        let update_matches = request.method != MessageMethod::Update
            || response.get("ts") == request.payload.get("ts");
        if response.get("ok") == Some(&Value::Bool(true))
            && timestamp.is_some()
            && channel_matches
            && update_matches
        {
            Self {
                status: DeliveryStatus::Confirmed,
                receipt: Some(response),
                reason: None,
            }
        } else {
            Self::uncertain("unconfirmed_response")
        }
    }
}

/// Working status is a separate effect: confirmation does not require a message timestamp.
pub struct StatusRequest {
    pub method: &'static str,
    pub payload: Value,
}
impl StatusRequest {
    pub fn new(destination: ReplyDestination, status: &str) -> Self {
        Self {
            method: "assistant.threads.setStatus",
            payload: json!({"channel_id":destination.channel_id,"thread_ts":destination.thread_ts,"status":status}),
        }
    }
    pub fn response(response: Value) -> DeliveryResult {
        let status = match response.get("ok") {
            Some(Value::Bool(true)) => DeliveryStatus::Confirmed,
            Some(Value::Bool(false)) => DeliveryStatus::Rejected,
            _ => return DeliveryResult::uncertain("unconfirmed_response"),
        };
        DeliveryResult {
            status,
            receipt: Some(response),
            reason: None,
        }
    }
}

/// Reconstruct a supplied plan through the same renderer/route validator used by new replies.
pub fn prepare_request(plan: &Value) -> Result<MessageRequest> {
    let invalid = || Error::InvalidInput("Expected a Slack message request".into());
    let payload = plan
        .get("payload")
        .filter(|value| value.is_object())
        .ok_or_else(invalid)?;
    let method = plan["method"].as_str().ok_or_else(invalid)?;
    if !matches!(method, "chat.postMessage" | "chat.update") {
        return Err(invalid());
    }
    let text = payload["text"].as_str().ok_or_else(invalid)?;
    let list = |key| -> Result<Vec<Map<String, Value>>> {
        match payload.get(key) {
            None => Ok(Vec::new()),
            Some(Value::Array(items)) => items
                .iter()
                .map(|item| item.as_object().cloned().ok_or_else(invalid))
                .collect(),
            _ => Err(invalid()),
        }
    };
    let optional = |key| -> Result<Option<&str>> {
        match payload.get(key) {
            None => Ok(None),
            Some(Value::String(value)) => Ok(Some(value)),
            _ => Err(invalid()),
        }
    };
    let timestamp = optional("ts")?;
    if (method == "chat.update") != timestamp.is_some() {
        return Err(invalid());
    }
    MessageRequest::new(
        &RichReply {
            text: text.into(),
            blocks: list("blocks")?,
            attachments: list("attachments")?,
        },
        payload["channel"].as_str().ok_or_else(invalid)?,
        optional("thread_ts")?,
        timestamp,
        payload.get("metadata"),
        false,
    )
}

/// Per-call state; concurrent reconciliations never share cursors or identities.
pub struct Reconciliation {
    destination: ReplyDestination,
    event_type: String,
    event_id: String,
    max_pages: usize,
    pages: usize,
    cursor: String,
    seen: HashSet<String>,
    result: DeliveryResult,
}
impl Reconciliation {
    pub fn new(
        channel: &str,
        thread: &str,
        event_type: &str,
        event_id: &str,
        max_pages: usize,
    ) -> Result<Self> {
        if !(1..=100).contains(&max_pages)
            || event_type.is_empty()
            || event_id.is_empty()
            || event_type.len() > 256
            || event_id.len() > 1024
        {
            return Err(Error::InvalidInput(
                "Expected bounded Slack reconciliation and metadata identity".into(),
            ));
        }
        Ok(Self {
            destination: ReplyDestination::validate(channel, Some(thread))?,
            event_type: event_type.into(),
            event_id: event_id.into(),
            max_pages,
            pages: 0,
            cursor: String::new(),
            seen: HashSet::new(),
            result: DeliveryResult::uncertain("not_found"),
        })
    }
    pub fn request(&self) -> Value {
        let mut payload = json!({"channel":self.destination.channel_id,"ts":self.destination.thread_ts,"limit":100,"include_all_metadata":"true"});
        if !self.cursor.is_empty() {
            payload["cursor"] = json!(self.cursor);
        }
        payload
    }
    pub fn fail(&mut self, reason: &str) {
        self.result = DeliveryResult::uncertain(reason);
    }
    pub fn finish(self) -> DeliveryResult {
        self.result
    }
    /// Returns true when no further page should be requested. Absence never permits replay.
    pub fn accept(&mut self, response: &Value) -> bool {
        self.pages += 1;
        if response.get("ok") != Some(&Value::Bool(true)) {
            self.fail("history_unavailable");
            return true;
        }
        let Some(messages) = response.get("messages").and_then(Value::as_array) else {
            self.fail("invalid_history");
            return true;
        };
        if messages.len() > 1000 {
            self.fail("invalid_history");
            return true;
        }
        let thread = self.destination.thread_ts.as_deref().unwrap_or_default();
        for message in messages {
            let Some(timestamp) = message["ts"].as_str().and_then(SlackTimestamp::parse) else {
                continue;
            };
            let Some(root) = SlackTimestamp::parse(thread) else {
                continue;
            };
            if timestamp.is_before(&root)
                || message
                    .get("thread_ts")
                    .is_some_and(|value| value.as_str() != Some(thread))
                || message
                    .get("channel")
                    .is_some_and(|value| value.as_str() != Some(&self.destination.channel_id))
            {
                continue;
            }
            let metadata = &message["metadata"];
            if metadata["event_type"].as_str() == Some(&self.event_type)
                && metadata["event_payload"]["id"].as_str() == Some(&self.event_id)
            {
                self.result = DeliveryResult {
                    status: DeliveryStatus::Confirmed,
                    receipt: Some(message.clone()),
                    reason: None,
                };
                return true;
            }
        }
        let cursor = response
            .get("response_metadata")
            .and_then(|value| value.get("next_cursor"));
        let next = match cursor {
            None | Some(Value::Null) => "",
            Some(Value::String(value)) => value.trim(),
            _ => {
                self.fail("invalid_cursor");
                return true;
            }
        };
        if next.is_empty() {
            if response["has_more"] == true {
                self.fail("missing_cursor");
            }
            return true;
        }
        if next.len() > 2048 || !self.seen.insert(next.into()) {
            self.fail("repeated_cursor");
            return true;
        }
        if self.pages >= self.max_pages {
            self.fail("page_limit");
            return true;
        }
        self.cursor = next.into();
        false
    }
}
