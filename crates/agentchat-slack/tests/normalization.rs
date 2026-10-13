use agentchat_core::{RichReply, Role};
use agentchat_slack::{events, rendering};
use serde_json::{json, Value};

fn event() -> Value {
    json!({"team_id":"T1","event_id":"Ev1","event":{
        "type":"app_mention","channel":"C1","user":"U1","ts":"1.000001",
        "text":"<@BOT> hi 🦀 <@OTHER>"}})
}

#[test]
fn shared_python_reference_fixtures_match_exactly() {
    let fixtures: Value =
        serde_json::from_str(include_str!("../../../conformance/slack-events.json")).unwrap();
    for case in fixtures.as_array().unwrap() {
        let actual = events::normalize(
            &case["payload"],
            case["bot_user_id"].as_str().unwrap(),
            case["workspace_id"].as_str().unwrap(),
        );
        assert_eq!(
            serde_json::to_value(actual).unwrap(),
            case["expected"],
            "{}",
            case["name"]
        );
    }
}

#[test]
fn messages_preserve_identity_and_only_strip_our_mention() {
    let payload = event();
    let message = events::normalize(&payload, "BOT", "T1").unwrap();
    assert_eq!(message.id.as_ref(), "slack:T1:C1:1.000001");
    assert_eq!(message.conversation_id.as_ref(), "slack:T1:C1:1.000001");
    assert_eq!(message.thread_id.as_deref(), Some("1.000001"));
    assert_eq!(message.text, "hi 🦀 <@OTHER>");
    assert!(message.addressed);
    assert_eq!(message.role, Role::User);
    let mut duplicate = payload;
    duplicate["event_id"] = json!("another-envelope");
    assert_eq!(
        events::normalize(&duplicate, "BOT", "T1").unwrap().id,
        message.id
    );
}

#[test]
fn direct_messages_share_conversation_but_preserve_explicit_threads() {
    let mut payload = event();
    payload["event"]["type"] = json!("message");
    payload["event"]["channel_type"] = json!("im");
    let first = events::normalize(&payload, "BOT", "T1").unwrap();
    assert_eq!(first.conversation_id.as_ref(), "slack:T1:C1");
    assert_eq!(first.thread_id, None);
    payload["event"]["ts"] = json!("2.000002");
    payload["event"]["thread_ts"] = json!("1.000001");
    let reply = events::normalize(&payload, "BOT", "T1").unwrap();
    assert_eq!(reply.conversation_id, first.conversation_id);
    assert_eq!(reply.thread_id.as_deref(), Some("1.000001"));
}

#[test]
fn ignores_foreign_workspace_self_bots_and_mutations() {
    assert!(events::normalize(&event(), "BOT", "T2").is_none());
    for (field, value) in [
        ("user", json!("BOT")),
        ("bot_id", json!("B1")),
        ("bot_profile", json!({})),
        ("subtype", json!("message_changed")),
        ("thread_ts", json!(123)),
        ("text", json!(123)),
        ("channel", json!("")),
    ] {
        let mut payload = event();
        payload["event"][field] = value;
        assert!(
            events::normalize(&payload, "BOT", "T1").is_none(),
            "{field}"
        );
    }
}

#[test]
fn unaddressed_channel_threads_require_subscription() {
    let mut payload = event();
    payload["event"]["type"] = json!("message");
    payload["event"]["channel_type"] = json!("channel");
    payload["event"]["text"] = json!("follow up");
    assert!(events::normalize(&payload, "BOT", "T1").is_none());
    payload["event"]["thread_ts"] = json!("0.001");
    let message = events::normalize(&payload, "BOT", "T1").unwrap();
    assert!(!message.addressed);
    assert_eq!(message.metadata["requires_subscription"], true);
    assert_eq!(message.conversation_id.as_ref(), "slack:T1:C1:0.001");
}

#[test]
fn file_only_messages_keep_bounded_sanitized_references() {
    let mut payload = event();
    payload["event"]["text"] = json!("<@BOT>");
    payload["event"]["subtype"] = json!("file_share");
    payload["event"]["files"] = json!([
        {"id":"F1","name":"/tmp/..\\report\u{200d}\u{e000}.txt","size":10,"mimetype":"text/plain","url_private":"secret"},
        {"id":"F1","name":"duplicate"}, {"id":"../unsafe"}, {"id":"F2","size":true},
        {"id":"F3","name":"..."}, {"id":"F4"}]);
    let message = events::normalize(&payload, "BOT", "T1").unwrap();
    assert_eq!(message.attachments.len(), 3);
    assert_eq!(message.attachments[0].name.as_deref(), Some("report.txt"));
    assert_eq!(message.attachments[1].size, None);
    assert_eq!(message.attachments[2].name.as_deref(), Some("attachment"));
    assert!(!serde_json::to_string(&message).unwrap().contains("secret"));
}

#[test]
fn rendering_preserves_unicode_and_reopens_code_fences() {
    let text = format!("```rust\n{}\n```", "🦀".repeat(7000));
    let blocks = rendering::text_blocks(&text).unwrap();
    assert_eq!(blocks.len(), 3);
    for block in &blocks {
        let section = block["text"]["text"].as_str().unwrap();
        assert!(section.chars().count() <= 3000);
        assert_eq!(section.matches("```").count() % 2, 0);
        assert_eq!(block["expand"], true);
        assert_eq!(block["text"]["verbatim"], true);
    }
    assert_eq!(
        blocks
            .iter()
            .map(|b| b["text"]["text"].as_str().unwrap().matches('🦀').count())
            .sum::<usize>(),
        7000
    );
    assert!(rendering::text_blocks("   ").is_err());
    assert!(rendering::text_blocks(&"x".repeat(2992 * 50 + 1)).is_err());
}

#[test]
fn rich_payload_expands_sections_without_overriding_explicit_behavior() {
    let reply = RichReply {
        text: "fallback".into(),
        blocks: vec![
            json!({"type":"section","text":{"type":"plain_text","text":"one"}})
                .as_object()
                .unwrap()
                .clone(),
            json!({"type":"section","expand":false})
                .as_object()
                .unwrap()
                .clone(),
        ],
        attachments: Vec::new(),
    };
    let payload = rendering::rich_payload(&reply).unwrap();
    assert_eq!(payload["blocks"][0]["expand"], true);
    assert_eq!(payload["blocks"][1]["expand"], false);
    assert_eq!(payload["parse"], "none");
}

#[test]
fn mirror_uses_plain_text_and_escaped_fallback() {
    let message = events::normalize(&event(), "BOT", "T1").unwrap();
    let payload = rendering::mirror_payload(&message, "web").unwrap();
    assert_eq!(payload["blocks"][1]["text"]["type"], "plain_text");
    assert!(payload["text"].as_str().unwrap().contains("&lt;@OTHER&gt;"));
    assert_eq!(payload["mrkdwn"], false);
    let mut too_long = message;
    too_long.text = "🦀".repeat(2801);
    assert!(rendering::mirror_payload(&too_long, "web").is_err());
}
