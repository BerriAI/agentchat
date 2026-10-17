use agentchat_slack::host::{
    ack_accepted, ack_request, ContextLimits, CoverageStatus, SlackHost, ThreadContextReader,
    ThreadOrigin,
};
use serde_json::json;

fn host() -> SlackHost {
    SlackHost {
        bot_user_id: "UBOT00001".into(),
        workspace_id: Some("TTEST0001".into()),
        exclude_prefixes: vec!["!aside".into()],
        ack_emoji: Some("eyes".into()),
    }
}
fn origin() -> ThreadOrigin {
    ThreadOrigin {
        team_id: "TTEST0001".into(),
        channel_id: "CTEST0001".into(),
        thread_ts: "1780000000.000001".into(),
        message_ts: "1780000000.000010".into(),
    }
}
fn reader(max_pages: usize, max_chars: usize) -> ThreadContextReader {
    ThreadContextReader::new(
        host(),
        origin(),
        ContextLimits {
            max_pages,
            max_chars,
            max_message_chars: 8000,
        },
    )
    .unwrap()
}
fn record(ts: &str, text: &str) -> serde_json::Value {
    json!({"ts":ts,"user":"UAUTHOR01","text":text})
}

#[test]
fn acknowledgement_payload_uses_the_message_timestamp() {
    let expected = json!({
        "channel":"CTEST0001",
        "name":"eyes",
        "timestamp":"1780000000.000010",
    });
    assert_eq!(
        ack_request("CTEST0001", "eyes", "1780000000.000010"),
        expected
    );
    assert_eq!(host().acknowledgement(&origin()), Some(expected));
}

#[test]
fn acknowledgement_can_be_disabled() {
    let mut host = host();
    host.ack_emoji = None;
    assert_eq!(host.acknowledgement(&origin()), None);
    host.ack_emoji = Some(String::new());
    assert_eq!(host.acknowledgement(&origin()), None);
}

#[test]
fn acknowledgement_acceptance_includes_already_reacted() {
    assert!(ack_accepted(&json!({"ok":true})));
    assert!(ack_accepted(&json!({"ok":false,"error":"already_reacted"})));
    assert!(!ack_accepted(&json!({"ok":false,"error":"missing_scope"})));
}

#[test]
fn parent_preview_survives_and_current_composer_text_is_not_repeated() {
    let mut read = reader(5, 40000);
    let root = json!({"ts":"1780000000.000001","user":"UAUTHOR01","text":"https://x.com/deepseek_ai/status/2111375640717033777?s=46 new deepseek","attachments":[{"text":"DeepSeek-V4.1-Flash-1017 is here!"}]});
    let current = json!({"ts":"1780000000.000010","user":"UAUTHOR01","text":"do we support this?","blocks":[{"type":"rich_text","text":"do we support this?"}],"attachments":[{"text":"current preview"}]});
    assert!(read.accept(&json!({"ok":true,"messages":[root,current],"has_more":false})));
    let context = read.finish();
    assert_eq!(context.status, CoverageStatus::Complete);
    let text = context
        .messages
        .iter()
        .map(|m| m.content.as_str())
        .collect::<String>();
    assert!(text.contains("DeepSeek-V4.1-Flash-1017"));
    assert!(text.contains("current preview"));
    assert!(!text.contains("do we support this?"));
}
#[test]
fn prior_pages_are_kept_when_provider_fails() {
    let mut read = reader(5, 40000);
    assert!(!read.accept(&json!({"ok":true,"messages":[record("1780000000.000001","root")],"has_more":true,"response_metadata":{"next_cursor":"next"}})));
    assert_eq!(read.request()["cursor"], "next");
    assert!(read.accept(&json!({"ok":false,"error":"missing_scope"})));
    let context = read.finish();
    assert_eq!(context.status, CoverageStatus::Partial);
    assert_eq!(context.messages.len(), 1);
    assert_eq!(context.error.as_deref(), Some("missing_scope"));
}
#[test]
fn bounded_context_keeps_root_and_recent_and_reports_missing_root() {
    let mut read = reader(1, 29);
    assert!(read.accept(&json!({"ok":true,"messages":[record("1780000000.000001","root"),record("1780000000.000002","old"),record("1780000000.000003","new")],"has_more":true,"response_metadata":{"next_cursor":"next"}})));
    let context = read.finish();
    assert_eq!(context.status, CoverageStatus::Partial);
    assert_eq!(context.messages.len(), 2);
    assert!(context.messages[0].content.contains("root"));
    assert!(context.messages[1].content.contains("new"));
    let mut read = reader(1, 100);
    read.accept(&json!({"ok":true,"messages":[],"has_more":false}));
    assert_eq!(read.finish().status, CoverageStatus::Partial);
}
#[test]
fn routing_keeps_literal_mentions_and_excludes_only_prefix_boundaries() {
    let host = host();
    for text in [
        "`<@UBOT00001>`",
        "```<@UBOT00001>",
        "> <@UBOT00001>",
        ">>> <@UBOT00001>\nrest",
    ] {
        let route = host.routing_text(text);
        assert_eq!(route.text, text);
        assert!(!route.mentioned);
    }
    assert!(host.routing_text("<@UBOT00001> !ASIDE private").excluded);
    assert!(!host.routing_text("!asideways").excluded);
}

#[test]
fn current_event_reference_is_bound_and_survives_read_failure() {
    let input = json!({"team_id":"TTEST0001","event":{"type":"app_mention","channel":"CTEST0001","ts":"1780000000.000010","thread_ts":"1780000000.000001","user":"UAUTHOR01","text":"<@UBOT00001> do we support this?","attachments":[{"text":"DeepSeek-V4.1-Flash-1017"}]}});
    let event = host().normalize_event(&input).unwrap().unwrap();
    let mut read = reader(5, 40000);
    let mut wrong = event.reference.clone();
    wrong.origin.message_ts = "1780000000.000009".into();
    assert!(read.seed(wrong, &event.user_id).is_err());
    read.seed(event.reference, &event.user_id).unwrap();
    read.fail("missing_scope");
    let context = read.finish();
    assert_eq!(context.status, CoverageStatus::Partial);
    assert!(context.messages[0]
        .content
        .contains("DeepSeek-V4.1-Flash-1017"));
    assert!(!context.messages[0].content.contains("do we support this?"));
}
