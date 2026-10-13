//! Authenticated Teams activity interpretation and continuation routing.
use crate::wire::{quote, strip, truthy};
use agentchat_core::{Attachment, Message, Role, Sender};
use serde_json::{json, Value};
pub fn text_payload(content: &str) -> Result<&str, &'static str> {
    if strip(content).is_empty() {
        return Err("Teams replies require nonempty text");
    }
    if content.len() > 24000 {
        return Err("Teams text replies must fit 24,000 UTF-8 bytes; send separate replies");
    }
    Ok(content)
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum ConversationKind {
    Personal,
    GroupChat,
    Channel,
}
impl ConversationKind {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "personal" => Some(Self::Personal),
            "groupChat" => Some(Self::GroupChat),
            "channel" => Some(Self::Channel),
            _ => None,
        }
    }
}
struct ConversationRoute<'a> {
    base_id: &'a str,
    root_id: Option<&'a str>,
}
impl<'a> ConversationRoute<'a> {
    fn from_activity(
        kind: ConversationKind,
        conversation_id: &'a str,
        activity_id: &'a str,
        reply_to: Option<&'a Value>,
    ) -> Option<Self> {
        if kind != ConversationKind::Channel {
            return Some(Self {
                base_id: conversation_id,
                root_id: None,
            });
        }
        let (base_id, suffix) = conversation_id
            .split_once(";messageid=")
            .unwrap_or((conversation_id, ""));
        let root_id = match reply_to {
            Some(value) if truthy(value) => value.as_str()?,
            _ if !suffix.is_empty() => suffix,
            _ => activity_id,
        };
        Some(Self {
            base_id,
            root_id: Some(root_id),
        })
    }
}
pub fn normalize(event: &Value, app_id: &str, tenant_id: &str) -> Option<Message> {
    if event.get("type")?.as_str() != Some("message")
        || event.get("channelId")?.as_str() != Some("msteams")
    {
        return None;
    }
    let conversation = event.get("conversation")?.as_object()?;
    let sender = event.get("from")?.as_object()?;
    let recipient = event.get("recipient")?.as_object()?;
    if event.get("channelData")?.get("tenant")?.get("id")?.as_str() != Some(tenant_id) {
        return None;
    }
    let recipient_id = recipient.get("id")?.as_str()?;
    if recipient_id != app_id && recipient_id != format!("28:{app_id}") {
        return None;
    }
    let activity_id = event.get("id")?.as_str()?;
    let sender_id = sender.get("id")?.as_str()?;
    let conversation_id = conversation.get("id")?.as_str()?;
    let service = event.get("serviceUrl")?.as_str()?;
    if [activity_id, sender_id, conversation_id, service]
        .iter()
        .any(|v| v.is_empty())
        || sender_id == recipient_id
        || sender.get("role").and_then(Value::as_str) == Some("bot")
    {
        return None;
    }
    let kind = ConversationKind::parse(conversation.get("conversationType")?.as_str()?)?;
    let mut text = match event.get("text") {
        Some(v) if truthy(v) => v.as_str()?.to_owned(),
        _ => String::new(),
    };
    let mut mentioned = false;
    for entity in event
        .get("entities")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if entity.get("type").and_then(Value::as_str) == Some("mention")
            && entity
                .get("mentioned")
                .and_then(|v| v.get("id"))
                .and_then(Value::as_str)
                == Some(recipient_id)
        {
            mentioned = true;
            if let Some(token) = entity.get("text").and_then(Value::as_str) {
                text = text.replace(token, "");
            }
        }
    }
    if kind != ConversationKind::Personal && !mentioned {
        return None;
    }
    let attachments = event
        .get("attachments")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .enumerate()
        .filter_map(|(index, a)| {
            a.as_object()?;
            Some(Attachment {
                id: a
                    .get("content")
                    .and_then(|v| v.get("uniqueId"))
                    .and_then(Value::as_str)
                    .filter(|v| !v.is_empty())
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("{activity_id}:{index}")),
                name: a.get("name").and_then(Value::as_str).map(str::to_owned),
                media_type: a
                    .get("contentType")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                size: None,
            })
        })
        .collect::<Vec<_>>();
    let text = strip(&text).to_owned();
    if text.is_empty() && attachments.is_empty() {
        return None;
    }
    let routing = ConversationRoute::from_activity(
        kind,
        conversation_id,
        activity_id,
        event.get("replyToId"),
    )?;
    let root = routing.root_id.map(str::to_owned);
    let base = routing.base_id;
    let mut identity = format!("{}:{}:{}", quote(app_id), quote(tenant_id), quote(base));
    if let Some(root) = &root {
        identity.push(':');
        identity.push_str(&quote(root));
    }
    let mut route = json!({"type":"message","channelId":"msteams","serviceUrl":service,"conversation":conversation,"from":sender,"recipient":recipient,"channelData":{"tenant":{"id":tenant_id}}});
    if let Some(root) = &root {
        route["id"] = root.clone().into();
    }
    Some(Message {
        id: format!(
            "teams:{app_id}:{tenant_id}:{}:{activity_id}",
            quote(conversation_id)
        )
        .into(),
        conversation_id: format!("teams:{identity}").into(),
        channel: "teams".into(),
        sender: Sender {
            id: sender_id.into(),
            display_name: sender
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_owned),
        },
        text,
        role: Role::User,
        metadata: json!({
            "app_id": app_id,
            "tenant_id": tenant_id,
            "activity_id": activity_id,
            "continuation_activity": route,
        })
        .as_object()?
        .clone(),
        thread_id: root,
        addressed: true,
        attachments,
    })
}
pub fn validate_service_url(raw: &str, allowed_host: bool) -> Result<(), &'static str> {
    let invalid = "Teams service URL is not an allowed HTTPS Microsoft endpoint";
    let url = url::Url::parse(raw).map_err(|_| invalid)?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.port().is_some_and(|p| p != 443)
        || !allowed_host
    {
        return Err(invalid);
    }
    Ok(())
}
#[derive(Debug, Clone)]
pub struct Destination {
    pub app_id: String,
    pub tenant_id: String,
    pub conversation_id: String,
    pub recipient: Sender,
    pub service_url: String,
    pub activity: Value,
}
impl Destination {
    pub fn from_source(source: &Message, app: &str, tenant: &str) -> Result<Self, &'static str> {
        let data = &source.metadata;
        let route = data.get("continuation_activity").filter(|v| v.is_object());
        if source.channel != "teams"
            || data.get("app_id").and_then(Value::as_str) != Some(app)
            || data.get("tenant_id").and_then(Value::as_str) != Some(tenant)
            || route.is_none()
        {
            return Err("Source does not belong to this Teams adapter");
        }
        let route = route.unwrap();
        let conversation = route
            .get("conversation")
            .and_then(|v| v.get("id"))
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty());
        let recipient = route
            .get("recipient")
            .and_then(|v| v.get("id"))
            .and_then(Value::as_str);
        if conversation.is_none()
            || !recipient.is_some_and(|id| id == app || id == format!("28:{app}"))
            || route
                .get("channelData")
                .and_then(|v| v.get("tenant"))
                .and_then(|v| v.get("id"))
                .and_then(Value::as_str)
                != Some(tenant)
        {
            return Err("Invalid Teams conversation reference");
        }
        Ok(Self {
            app_id: app.into(),
            tenant_id: tenant.into(),
            conversation_id: conversation.unwrap().into(),
            recipient: Sender {
                id: recipient.unwrap().into(),
                display_name: route
                    .get("recipient")
                    .and_then(|v| v.get("name"))
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            },
            service_url: route
                .get("serviceUrl")
                .and_then(Value::as_str)
                .unwrap_or("")
                .into(),
            activity: route.clone(),
        })
    }
    pub fn confirmed_reply(
        &self,
        source: &Message,
        content: String,
        receipt: &str,
    ) -> Result<Message, &'static str> {
        if receipt.is_empty() {
            return Err("Teams did not return a message receipt");
        }
        let mut result = source.clone();
        result.id = format!(
            "teams:{}:{}:{}:{receipt}",
            self.app_id,
            self.tenant_id,
            quote(&self.conversation_id)
        )
        .into();
        result.sender = self.recipient.clone();
        result.text = content;
        result.role = Role::Assistant;
        result.addressed = true;
        result.attachments.clear();
        result.metadata.insert("activity_id".into(), receipt.into());
        Ok(result)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn enforce_bytes() {
        assert!(text_payload(&"😀".repeat(6000)).is_ok());
        assert!(text_payload(&"😀".repeat(6001)).is_err());
    }
    #[test]
    fn validate_before_connector() {
        assert!(validate_service_url("https://smba.trafficmanager.net/amer/", true).is_ok());
        assert!(validate_service_url("http://smba.trafficmanager.net/amer/", true).is_err());
        assert!(validate_service_url("https://evil.test/", false).is_err());
    }

    #[test]
    fn thread_roots_and_tenant_identity_survive_continuations() {
        let event = json!({
            "type": "message",
            "channelId": "msteams",
            "id": "first",
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "conversation": {
                "id": "conversation",
                "conversationType": "channel",
            },
            "from": {"id": "alice", "name": "雪"},
            "recipient": {"id": "28:app", "name": "Bot"},
            "channelData": {"tenant": {"id": "tenant"}},
            "text": "<at>Bot</at> hello",
            "entities": [{
                "type": "mention",
                "text": "<at>Bot</at>",
                "mentioned": {"id": "28:app"},
            }],
        });
        let first = normalize(&event, "app", "tenant").unwrap();
        let mut event = event;
        event["id"] = "second".into();
        event["conversation"]["id"] = "conversation;messageid=first".into();
        let second = normalize(&event, "app", "tenant").unwrap();
        assert_eq!(first.conversation_id, second.conversation_id);
        assert_eq!(second.thread_id.as_deref(), Some("first"));
        assert_eq!(second.text, "hello");
        let route = Destination::from_source(&second, "app", "tenant").unwrap();
        assert_eq!(route.activity["id"], "first");
        let response = route
            .confirmed_reply(&second, "answer".into(), "sent")
            .unwrap();
        assert_eq!(response.thread_id, second.thread_id);
        assert_eq!(response.sender.id, "28:app");
        assert!(Destination::from_source(&second, "app", "other").is_err());
        assert!(normalize(&event, "app", "other").is_none());
    }
}
