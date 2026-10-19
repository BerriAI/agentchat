//! Validated Slack message requests for hosts that own durable delivery.

use crate::{policy::ReplyDestination, rendering, route::SlackTimestamp};
use agentchat_core::{Error, Result, RichReply};
use serde::Serialize;
use serde_json::{json, Value};
use url::Url;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum MessageMethod {
    #[serde(rename = "chat.postMessage")]
    Post,
    #[serde(rename = "chat.update")]
    Update,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlackAuthor {
    pub username: String,
    pub icon_url: Option<String>,
}

impl SlackAuthor {
    fn validated(&self) -> Result<Self> {
        let username = self
            .username
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if username.is_empty() || username.chars().count() > 80 {
            return Err(Error::InvalidInput(
                "Slack author username must contain 1 to 80 characters".into(),
            ));
        }
        if let Some(icon_url) = &self.icon_url {
            let parsed = Url::parse(icon_url).map_err(|_| {
                Error::InvalidInput("Slack author icon_url must be an HTTPS URL".into())
            })?;
            if icon_url.chars().count() > 2048
                || parsed.scheme() != "https"
                || parsed.host_str().is_none()
                || !parsed.username().is_empty()
                || parsed.password().is_some()
            {
                return Err(Error::InvalidInput(
                    "Slack author icon_url must be an HTTPS URL without userinfo, at most 2048 characters"
                        .into(),
                ));
            }
        }
        Ok(Self {
            username,
            icon_url: self.icon_url.clone(),
        })
    }
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
        author: Option<&SlackAuthor>,
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
        let author = author.map(SlackAuthor::validated).transpose()?;
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
            if let Some(author) = author {
                payload["username"] = json!(author.username);
                if let Some(icon_url) = author.icon_url {
                    payload["icon_url"] = json!(icon_url);
                }
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
            None,
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
            assert!(
                MessageRequest::new(&reply(), channel, thread, message, None, None, true).is_err()
            );
        }
        assert!(
            MessageRequest::new(&reply(), "C123", None, None, Some(&json!([])), None, true)
                .is_err()
        );
    }

    #[test]
    fn preserves_explicit_empty_blocks_for_card_only_and_plain_updates() {
        let request =
            MessageRequest::new(&reply(), "C123", None, Some("123.002"), None, None, false)
                .unwrap();
        assert_eq!(request.payload["blocks"], json!([]));
        assert_eq!(request.payload["attachments"], json!([]));
    }

    #[test]
    fn posts_with_a_validated_author_override() {
        let author = SlackAuthor {
            username: "  Ishaan \n Jaffer ".into(),
            icon_url: Some("https://example.com/avatar.png".into()),
        };
        let request =
            MessageRequest::new(&reply(), "C123", None, None, None, Some(&author), true).unwrap();
        assert_eq!(request.payload["username"], "Ishaan Jaffer");
        assert_eq!(
            request.payload["icon_url"],
            "https://example.com/avatar.png"
        );
    }

    #[test]
    fn updates_ignore_author_overrides() {
        let author = SlackAuthor {
            username: "Ishaan".into(),
            icon_url: Some("https://example.com/avatar.png".into()),
        };
        let request = MessageRequest::new(
            &reply(),
            "C123",
            None,
            Some("123.002"),
            None,
            Some(&author),
            true,
        )
        .unwrap();
        assert_eq!(request.method, MessageMethod::Update);
        assert!(request.payload.get("username").is_none());
        assert!(request.payload.get("icon_url").is_none());
    }

    #[test]
    fn rejects_invalid_author_overrides() {
        for author in [
            SlackAuthor {
                username: " \n\t ".into(),
                icon_url: None,
            },
            SlackAuthor {
                username: "Ishaan".into(),
                icon_url: Some("http://example.com/avatar.png".into()),
            },
            SlackAuthor {
                username: "Ishaan".into(),
                icon_url: Some("https://user:pass@example.com/avatar.png".into()),
            },
        ] {
            assert!(
                MessageRequest::new(&reply(), "C123", None, None, None, Some(&author), true)
                    .is_err()
            );
        }
    }
}
