//! WhatsApp Cloud API normalization, routing, and authentication policy.
use agentchat_core::{Attachment, Message, Role, Sender};
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;

pub fn validate_config(
    phone: &str,
    token: &str,
    secret: &str,
    verify: &str,
    version: &str,
) -> Result<(), &'static str> {
    if [phone, token, secret, verify]
        .iter()
        .any(|v| v.trim().is_empty())
    {
        return Err("WhatsApp phone_number_id, access_token, app_secret, verify_token required");
    }
    if !digits(phone) {
        return Err("WhatsApp phone_number_id must be the numeric API ID");
    }
    let valid = version
        .strip_prefix('v')
        .and_then(|v| v.split_once('.'))
        .is_some_and(|(major, minor)| digits(major) && !major.starts_with('0') && digits(minor));
    if !valid {
        return Err("WhatsApp api_version must have the form v23.0");
    }
    Ok(())
}
fn digits(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|c| c.is_ascii_digit())
}
fn items(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or_default()
}
pub fn normalize(payload: &Value, phone: &str) -> Vec<Message> {
    let mut messages = Vec::new();
    if payload["object"] != "whatsapp_business_account" {
        return messages;
    }
    for entry in items(&payload["entry"]) {
        for change in items(&entry["changes"]) {
            let value = &change["value"];
            if change["field"] != "messages"
                || value["messaging_product"] != "whatsapp"
                || value["metadata"]["phone_number_id"] != phone
            {
                continue;
            }
            for event in items(&value["messages"]) {
                let (Some(sender), Some(id), Some(kind)) = (
                    event["from"].as_str(),
                    event["id"].as_str(),
                    event["type"].as_str(),
                ) else {
                    continue;
                };
                if !digits(sender) || id.is_empty() {
                    continue;
                }
                let mut attachments = Vec::new();
                let empty = json!("");
                let text = match kind {
                    "text" => event["text"].get("body").unwrap_or(&empty),
                    "image" | "audio" | "video" | "document" | "sticker" => {
                        let media = &event[kind];
                        if let Some(id) = media["id"].as_str().filter(|v| !v.is_empty()) {
                            attachments.push(Attachment {
                                id: id.into(),
                                name: media["filename"].as_str().map(str::to_owned),
                                media_type: media["mime_type"].as_str().map(str::to_owned),
                                size: None,
                            });
                        }
                        media.get("caption").unwrap_or(&empty)
                    }
                    "button" => &event["button"]["text"],
                    "interactive" => {
                        let interactive = &event["interactive"];
                        if interactive["button_reply"]
                            .as_object()
                            .is_some_and(|v| !v.is_empty())
                        {
                            &interactive["button_reply"]["title"]
                        } else {
                            &interactive["list_reply"]["title"]
                        }
                    }
                    _ => &Value::Null,
                };
                let Some(text) = text.as_str() else {
                    continue;
                };
                if text.trim().is_empty() && attachments.is_empty() {
                    continue;
                }
                let display_name = items(&value["contacts"])
                    .iter()
                    .rev()
                    .find(|v| v["wa_id"] == sender)
                    .and_then(|v| v["profile"]["name"].as_str())
                    .map(str::to_owned);
                messages.push(Message {
                    id: format!("whatsapp:{phone}:{id}").into(),
                    conversation_id: format!("whatsapp:{phone}:{sender}").into(),
                    channel: "whatsapp".into(),
                    sender: Sender {
                        id: sender.into(),
                        display_name,
                    },
                    text: text.into(),
                    role: Role::User,
                    thread_id: None,
                    addressed: true,
                    attachments,
                    metadata: [
                        ("phone_number_id".into(), json!(phone)),
                        ("message_id".into(), json!(id)),
                        ("wa_id".into(), json!(sender)),
                        ("message_type".into(), json!(kind)),
                    ]
                    .into_iter()
                    .collect(),
                });
            }
        }
    }
    messages
}
pub struct Destination {
    pub recipient: String,
    pub message_id: String,
}
pub fn destination(source: &Message, phone: &str) -> Result<Destination, &'static str> {
    let invalid = "Source does not belong to this WhatsApp adapter";
    if source.channel != "whatsapp" || source.metadata.get("phone_number_id") != Some(&json!(phone))
    {
        return Err(invalid);
    }
    let recipient = source
        .metadata
        .get("wa_id")
        .and_then(Value::as_str)
        .filter(|value| digits(value))
        .ok_or(invalid)?;
    let id = source
        .metadata
        .get("message_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or(invalid)?;
    Ok(Destination {
        recipient: recipient.into(),
        message_id: id.into(),
    })
}

pub fn text_payload(
    content: Option<&str>,
    recipient: &str,
    id: &str,
) -> Result<Value, &'static str> {
    let content = content
        .filter(|s| !s.trim().is_empty())
        .ok_or("WhatsApp replies require nonempty text")?;
    if content.chars().count() > 4096 {
        return Err("WhatsApp replies must fit 4,096 characters; send separate replies");
    }
    Ok(
        json!({"messaging_product":"whatsapp","recipient_type":"individual","to":recipient,"type":"text","text":{"body":content,"preview_url":false},"context":{"message_id":id}}),
    )
}
pub fn receipt(
    source: &Message,
    phone: &str,
    content: &str,
    result: &Value,
) -> Result<Message, &'static str> {
    let receipts = items(&result["messages"]);
    let id = if receipts.len() == 1 {
        receipts[0]["id"].as_str().filter(|s| !s.is_empty())
    } else {
        None
    }
    .ok_or("WhatsApp did not return a message receipt")?;
    let mut sent = source.clone();
    sent.id = format!("whatsapp:{phone}:{id}").into();
    sent.sender = Sender {
        id: phone.into(),
        display_name: None,
    };
    sent.text = content.into();
    sent.role = Role::Assistant;
    sent.addressed = true;
    sent.thread_id = None;
    sent.attachments.clear();
    sent.metadata.insert("message_id".into(), json!(id));
    Ok(sent)
}
pub fn verify_signature(secret: &str, raw: &[u8], supplied: &str) -> bool {
    let Some(signature) = supplied
        .strip_prefix("sha256=")
        .and_then(|v| hex::decode(v).ok())
    else {
        return false;
    };
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(secret.as_bytes()) else {
        return false;
    };
    mac.update(raw);
    mac.verify_slice(&signature).is_ok()
}
pub fn verify_challenge(mode: &str, challenge: &str, supplied: &str, expected: &str) -> bool {
    mode == "subscribe"
        && !challenge.is_empty()
        && constant_time_eq(supplied.as_bytes(), expected.as_bytes())
}
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(b"AgentChat constant time comparison") else {
        return false;
    };
    mac.update(left);
    let digest = mac.finalize().into_bytes();
    let Ok(mut check) = Hmac::<Sha256>::new_from_slice(b"AgentChat constant time comparison")
    else {
        return false;
    };
    check.update(right);
    check.verify_slice(&digest).is_ok()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validation_and_payload() {
        assert!(validate_config("123", "t", "s", "v", "v23.0").is_ok());
        assert!(validate_config("١", "t", "s", "v", "v23.0").is_err());
        assert!(text_payload(Some(&"😀".repeat(4096)), "1", "m").is_ok());
        assert!(text_payload(None, "1", "m").is_err());
    }
    #[test]
    fn signature_is_verified() {
        let mut mac = Hmac::<Sha256>::new_from_slice(b"s").unwrap();
        mac.update(b"body");
        let signature = format!("sha256={}", hex::encode(mac.finalize().into_bytes()));
        assert!(verify_signature("s", b"body", &signature));
        assert!(!verify_signature("s", b"body ", &signature));
    }
}
