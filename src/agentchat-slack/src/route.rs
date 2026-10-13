//! Slack destinations are validated once here, then consumed by provider operations.

use agentchat_core::{Error, Message, Result};
use serde::{Serialize, Serializer};
use serde_json::Value;

#[derive(Clone, Debug)]
pub(crate) struct Destination {
    pub(crate) channel_id: String,
    pub(crate) thread_timestamp: Option<SlackTimestamp>,
    pub(crate) team_id: String,
}

#[derive(Clone, Debug)]
pub(crate) struct SlackTimestamp {
    raw: String,
    seconds: String,
    fraction: String,
}

impl SlackTimestamp {
    pub(crate) fn parse(value: &str) -> Option<Self> {
        let (seconds, fraction) = value.split_once('.')?;
        if seconds.is_empty()
            || fraction.is_empty()
            || !seconds.bytes().all(|c| c.is_ascii_digit())
            || !fraction.bytes().all(|c| c.is_ascii_digit())
        {
            return None;
        }
        Some(Self {
            raw: value.into(),
            seconds: seconds.trim_start_matches('0').into(),
            fraction: fraction.trim_end_matches('0').into(),
        })
    }
    pub(crate) fn as_str(&self) -> &str {
        &self.raw
    }
    pub(crate) fn is_before(&self, other: &Self) -> bool {
        self.seconds
            .len()
            .cmp(&other.seconds.len())
            .then_with(|| self.seconds.cmp(&other.seconds))
            .then_with(|| self.fraction.cmp(&other.fraction))
            .is_lt()
    }
}

impl std::fmt::Display for SlackTimestamp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.raw.fmt(f)
    }
}

impl Serialize for SlackTimestamp {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.raw)
    }
}

impl Destination {
    pub(crate) fn from_message(source: &Message, workspace_id: &str) -> Result<Self> {
        let metadata = &source.metadata;
        let text = |key| {
            metadata
                .get(key)
                .and_then(Value::as_str)
                .filter(|v| !v.is_empty())
        };
        let invalid = || {
            Error::InvalidInput(
                "Expected a source in the configured Slack workspace with a valid destination"
                    .into(),
            )
        };
        if source.channel != "slack" || text("team_id") != Some(workspace_id) {
            return Err(invalid());
        }
        let channel_id = text("channel_id").ok_or_else(invalid)?;
        super::policy::validate_channel(channel_id).map_err(|_| invalid())?;
        let _message_timestamp = text("message_timestamp")
            .and_then(SlackTimestamp::parse)
            .ok_or_else(invalid)?;
        let thread_timestamp = match metadata.get("reply_thread_timestamp") {
            None | Some(Value::Null) => None,
            Some(Value::String(thread)) => Some(SlackTimestamp::parse(thread).ok_or_else(invalid)?),
            _ => return Err(invalid()),
        };
        Ok(Self {
            channel_id: channel_id.into(),
            thread_timestamp,
            team_id: workspace_id.into(),
        })
    }
}
