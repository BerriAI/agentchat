//! Signed HTTP ingress without application authorization or durable admission.
use hmac::{Hmac, Mac};
use serde::Serialize;
use serde_json::Value;
use sha2::Sha256;
use std::{error::Error, fmt};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WebhookError {
    Signature,
    Payload,
    TooLarge,
}
impl fmt::Display for WebhookError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Signature => "Invalid Slack signature",
            Self::Payload => "Invalid Slack payload",
            Self::TooLarge => "Slack payload is too large",
        })
    }
}
impl Error for WebhookError {}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum WebhookEvent {
    Challenge { challenge: String, payload: Value },
    Event { payload: Value },
    Ignored { payload: Value },
}

/// Verification precedes parsing. Workspace authorization belongs to the host.
pub struct SlackWebhook {
    secret: Vec<u8>,
}
impl SlackWebhook {
    pub fn new(secret: &str) -> Self {
        Self {
            secret: secret.as_bytes().to_vec(),
        }
    }

    pub fn verify(
        &self,
        raw: &[u8],
        timestamp: &str,
        signature: &str,
        now: f64,
    ) -> Result<(), WebhookError> {
        let stamp = timestamp
            .parse::<u64>()
            .map_err(|_| WebhookError::Signature)?;
        if self.secret.is_empty() || !now.is_finite() || (now - stamp as f64).abs() > 300.0 {
            return Err(WebhookError::Signature);
        }
        let hex = signature
            .strip_prefix("v0=")
            .ok_or(WebhookError::Signature)?;
        if hex.len() != 64 {
            return Err(WebhookError::Signature);
        }
        let mut supplied = [0u8; 32];
        for (index, pair) in hex.as_bytes().chunks_exact(2).enumerate() {
            let digit = |value: u8| match value {
                b'0'..=b'9' => Some(value - b'0'),
                b'a'..=b'f' => Some(value - b'a' + 10),
                _ => None,
            };
            supplied[index] = digit(pair[0]).ok_or(WebhookError::Signature)? * 16
                + digit(pair[1]).ok_or(WebhookError::Signature)?;
        }
        let mut mac =
            Hmac::<Sha256>::new_from_slice(&self.secret).map_err(|_| WebhookError::Signature)?;
        mac.update(b"v0:");
        mac.update(timestamp.as_bytes());
        mac.update(b":");
        mac.update(raw);
        mac.verify_slice(&supplied)
            .map_err(|_| WebhookError::Signature)
    }

    fn authenticated(
        &self,
        raw: &[u8],
        timestamp: &str,
        signature: &str,
        now: f64,
        max_bytes: usize,
    ) -> Result<(), WebhookError> {
        if max_bytes == 0 || raw.len() > max_bytes {
            return Err(WebhookError::TooLarge);
        }
        self.verify(raw, timestamp, signature, now)
    }

    pub fn event(
        &self,
        raw: &[u8],
        timestamp: &str,
        signature: &str,
        now: f64,
        max_bytes: usize,
    ) -> Result<WebhookEvent, WebhookError> {
        self.authenticated(raw, timestamp, signature, now, max_bytes)?;
        let payload = object(raw)?;
        match payload.get("type").and_then(Value::as_str) {
            Some("url_verification") => {
                let challenge = payload["challenge"]
                    .as_str()
                    .ok_or(WebhookError::Payload)?
                    .to_owned();
                Ok(WebhookEvent::Challenge { challenge, payload })
            }
            Some("event_callback") => {
                if !["team_id", "event_id"].iter().all(|key| {
                    payload[*key].as_str().is_some_and(|value| {
                        !value.is_empty()
                            && value.len() <= 100
                            && value.bytes().all(|byte| {
                                byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
                            })
                    })
                }) || !payload["event"].is_object()
                {
                    return Err(WebhookError::Payload);
                }
                Ok(WebhookEvent::Event { payload })
            }
            _ => Ok(WebhookEvent::Ignored { payload }),
        }
    }

    pub fn interaction(
        &self,
        raw: &[u8],
        timestamp: &str,
        signature: &str,
        now: f64,
        max_bytes: usize,
    ) -> Result<Value, WebhookError> {
        self.authenticated(raw, timestamp, signature, now, max_bytes)?;
        let input = std::str::from_utf8(raw).map_err(|_| WebhookError::Payload)?;
        // Reject malformed escapes and excessive field counts before decoding.
        let fields: Vec<_> = input.split('&').take(101).collect();
        if fields.len() > 100 || fields.iter().any(|field| !field.contains('=')) {
            return Err(WebhookError::Payload);
        }
        let mut decoded = Vec::with_capacity(raw.len());
        let mut bytes = input.as_bytes().iter();
        while let Some(byte) = bytes.next() {
            if *byte == b'%' {
                let high = bytes.next().and_then(|value| (*value as char).to_digit(16));
                let low = bytes.next().and_then(|value| (*value as char).to_digit(16));
                decoded.push(
                    (high.ok_or(WebhookError::Payload)? * 16 + low.ok_or(WebhookError::Payload)?)
                        as u8,
                );
            } else {
                decoded.push(*byte);
            }
        }
        std::str::from_utf8(&decoded).map_err(|_| WebhookError::Payload)?;
        let mut payload = None;
        for (key, value) in url::form_urlencoded::parse(raw) {
            if key == "payload" {
                if payload.is_some() {
                    return Err(WebhookError::Payload);
                }
                payload = Some(object(value.as_bytes())?);
            }
        }
        payload.ok_or(WebhookError::Payload)
    }
}
fn object(raw: &[u8]) -> Result<Value, WebhookError> {
    let value: Value = serde_json::from_slice(raw).map_err(|_| WebhookError::Payload)?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(WebhookError::Payload)
    }
}
