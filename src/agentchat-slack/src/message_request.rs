//! Validated Slack message requests for hosts that own durable delivery.

use crate::{policy::ReplyDestination, rendering, route::SlackTimestamp};
use agentchat_core::{Error, Result, RichReply};
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum MessageMethod {
    #[serde(rename = "chat.postMessage")]
    Post,
    #[serde(rename = "chat.update")]
    Update,
}

/// A prepared request, with no transport, retry, authorization or queue policy.
#[derive(Clone, Debug, Serialize)]
pub struct MessageRequest {
    pub method: MessageMethod,
    pub payload: Value,
}

impl MessageRequest {
    pub fn new(
        reply: &RichReply,
        channel_id: &str,
        thread_ts: Option<&str>,
        message_ts: Option<&str>,
        metadata: Option<&Value>,
        fallback_blocks: bool,
    ) -> Result<Self> {
        let destination = ReplyDestination::validate(channel_id, thread_ts)?;
        let timestamp = message_ts
            .map(|value| {
                SlackTimestamp::parse(value)
                    .ok_or_else(|| Error::InvalidInput("Expected a Slack message timestamp".into()))
            })
            .transpose()?;
        if metadata.is_some_and(|value| !value.is_object()) {
            return Err(Error::InvalidInput(
                "Slack message metadata must be an object".into(),
            ));
        }
        let mut payload = rendering::rich_payload_with_fallback_blocks(reply, fallback_blocks)?;
        payload["channel"] = json!(destination.channel_id);
        let method = if let Some(timestamp) = timestamp {
            payload["ts"] = json!(timestamp);
            // Slack otherwise retains attachments from the message being replaced.
            payload["attachments"] = payload
                .get("attachments")
                .cloned()
                .unwrap_or_else(|| json!([]));
            MessageMethod::Update
        } else {
            if let Some(thread) = destination.thread_ts {
                payload["thread_ts"] = json!(thread);
            }
            MessageMethod::Post
        };
        if let Some(metadata) = metadata {
            payload["metadata"] = metadata.clone();
        }
        Ok(Self { method, payload })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reply() -> RichReply {
        RichReply {
            text: "Ready".into(),
            blocks: Vec::new(),
            attachments: Vec::new(),
        }
    }

    #[test]
    fn posts_to_the_validated_thread_with_safe_rendering() {
        let metadata = json!({"event_type":"host_delivery","event_payload":{"id":"saved-1"}});
        let request = MessageRequest::new(
            &reply(),
            "C123",
            Some("123.001"),
            None,
            Some(&metadata),
            true,
        )
        .unwrap();
        assert_eq!(request.method, MessageMethod::Post);
        assert_eq!(request.payload["channel"], "C123");
        assert_eq!(request.payload["thread_ts"], "123.001");
        assert!(request.payload.get("ts").is_none());
        assert_eq!(request.payload["metadata"], metadata);
        assert_eq!(request.payload["blocks"][0]["expand"], true);
        assert_eq!(request.payload["unfurl_links"], false);
        assert_eq!(request.payload["parse"], "none");
        assert_eq!(request.payload["link_names"], false);
    }

    #[test]
    fn updates_replace_old_attachments_and_do_not_move_the_message() {
        let request = MessageRequest::new(
            &reply(),
            "D123",
            Some("123.001"),
            Some("123.002"),
            None,
            true,
        )
        .unwrap();
        assert_eq!(request.method, MessageMethod::Update);
        assert_eq!(request.payload["ts"], "123.002");
        assert!(request.payload.get("thread_ts").is_none());
        assert_eq!(request.payload["attachments"], json!([]));
        assert!(!request.payload["blocks"].as_array().unwrap().is_empty());
    }

    #[test]
    fn rejects_invalid_destinations_and_metadata_before_transport() {
        for (channel, thread, message) in [
            ("other", None, None),
            ("C123", Some("bad"), None),
            ("C123", None, Some("bad")),
        ] {
            assert!(MessageRequest::new(&reply(), channel, thread, message, None, true).is_err());
        }
        assert!(MessageRequest::new(&reply(), "C123", None, None, Some(&json!([])), true).is_err());
    }

    #[test]
    fn preserves_explicit_empty_blocks_for_card_only_and_plain_updates() {
        let request =
            MessageRequest::new(&reply(), "C123", None, Some("123.002"), None, false).unwrap();
        assert_eq!(request.payload["blocks"], json!([]));
        assert_eq!(request.payload["attachments"], json!([]));
    }
}
