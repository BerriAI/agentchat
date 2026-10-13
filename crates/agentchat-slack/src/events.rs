//! Pure Slack event normalization. No authentication, downloads, or network I/O.

use agentchat_core::{Attachment, Message, Role, Sender};
use serde_json::{json, Value};
use unicode_general_category::{get_general_category, GeneralCategory};

pub(crate) fn nonempty(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.is_empty())
}

pub(crate) fn trim_python(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\u{001c}'..='\u{001f}'))
}

pub(crate) fn valid_file_id(id: &str) -> bool {
    (2..=31).contains(&id.len())
        && id.starts_with('F')
        && id
            .bytes()
            .skip(1)
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
}

pub(crate) fn filename(value: &str) -> String {
    let normalized = value.replace('\\', "/");
    let name: String = normalized
        .rsplit('/')
        .next()
        .unwrap_or_default()
        .chars()
        .filter(|c| {
            !matches!(
                get_general_category(*c),
                GeneralCategory::Control
                    | GeneralCategory::Format
                    | GeneralCategory::Surrogate
                    | GeneralCategory::PrivateUse
                    | GeneralCategory::Unassigned
            )
        })
        .collect();
    let name: String = name.trim_matches([' ', '.']).chars().take(180).collect();
    if name.is_empty() {
        "attachment".into()
    } else {
        name
    }
}

/// Normalize at most five file references. Event URLs and credentials are discarded.
pub fn attachments(files: &Value) -> Vec<Attachment> {
    let Some(files) = files.as_array() else {
        return Vec::new();
    };
    let mut selected: Vec<Attachment> = Vec::new();
    for file in files.iter().take(5) {
        let Some(id) = nonempty(&file["id"]).filter(|id| valid_file_id(id)) else {
            continue;
        };
        if selected.iter().any(|item| item.id == id) {
            continue;
        }
        selected.push(Attachment {
            id: id.into(),
            name: file["name"].as_str().map(filename),
            media_type: file["mimetype"]
                .as_str()
                .map(|v| v.chars().take(255).collect()),
            size: file["size"].as_u64(),
        });
    }
    selected
}

/// Decode an authenticated Events API payload without doing I/O.
/// Untagged thread replies are marked for the adapter's subscription check.
pub fn normalize(payload: &Value, bot_user_id: &str, workspace_id: &str) -> Option<Message> {
    let event = payload.get("event")?.as_object()?;
    let get = |key: &str| event.get(key).unwrap_or(&Value::Null);
    let event_type = get("type").as_str()?;
    let direct = event_type == "message" && get("channel_type") == "im";
    if !get("bot_id").is_null()
        || !get("bot_profile").is_null()
        || (!get("subtype").is_null() && get("subtype") != "file_share")
    {
        return None;
    }
    let user = nonempty(get("user"))?;
    if user == bot_user_id {
        return None;
    }
    let channel = nonempty(get("channel"))?;
    let timestamp = nonempty(get("ts"))?;
    let text = event.get("text").map_or(Some(""), Value::as_str)?;
    let files = attachments(get("files"));
    if trim_python(text).is_empty() && files.is_empty() {
        return None;
    }
    let team = nonempty(&payload["team_id"])?;
    if team != workspace_id {
        return None;
    }
    let supplied_thread = if get("thread_ts").is_null() {
        None
    } else {
        Some(nonempty(get("thread_ts"))?)
    };
    let root = supplied_thread.unwrap_or(timestamp);
    let mention = format!("<@{bot_user_id}>");
    let channel_message = event_type == "message"
        && matches!(get("channel_type").as_str(), Some("channel" | "group"));
    let mentioned = event_type == "app_mention" || (channel_message && text.contains(&mention));
    if !(direct || mentioned || (channel_message && supplied_thread.is_some())) {
        return None;
    }
    let without_mention = text.replace(&mention, "");
    let normalized_text = trim_python(&without_mention).to_owned();
    if normalized_text.is_empty() && files.is_empty() {
        return None;
    }
    let thread = if direct { supplied_thread } else { Some(root) };
    let event_id = payload["event_id"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| format!("{channel}:{timestamp}"));
    Some(Message {
        id: format!("slack:{team}:{channel}:{timestamp}").into(),
        conversation_id: if direct { format!("slack:{team}:{channel}") }
            else { format!("slack:{team}:{channel}:{root}") }.into(),
        channel: "slack".into(),
        sender: Sender { id: user.into(), display_name: None },
        text: normalized_text,
        role: Role::User,
        thread_id: thread.map(str::to_owned),
        addressed: direct || mentioned,
        attachments: files,
        metadata: json!({
            "channel_id": channel, "team_id": team, "event_id": event_id,
            "channel_type": if direct { Value::String("im".into()) } else { event.get("channel_type").cloned().unwrap_or_else(|| Value::String("channel".into())) },
            "reply_thread_timestamp": thread, "message_timestamp": timestamp,
            "requires_subscription": !direct && !mentioned,
        }).as_object()?.clone(),
    })
}
