use agentchat_core::RichReply;
use agentchat_slack::{
    delivery::{prepare_request, DeliveryResult, DeliveryStatus, Reconciliation},
    message_request::MessageRequest,
    webhook::{SlackWebhook, WebhookError, WebhookEvent},
};
use hmac::{Hmac, Mac};
use serde_json::json;
use sha2::Sha256;

fn signature(raw: &[u8]) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(b"secret").unwrap();
    mac.update(b"v0:1000:");
    mac.update(raw);
    format!("v0={:x}", mac.finalize().into_bytes())
}
#[test]
fn signed_envelope_and_challenge_are_framework_independent() {
    let webhook = SlackWebhook::new("secret");
    let raw =
        br#"{"type":"event_callback","team_id":"T1","event_id":"Ev1","event":{"type":"message"}}"#;
    assert!(matches!(
        webhook
            .event(raw, "1000", &signature(raw), 1000.0, 262144)
            .unwrap(),
        WebhookEvent::Event { .. }
    ));
    let raw = br#"{"type":"url_verification","challenge":"confirm"}"#;
    assert!(
        matches!(webhook.event(raw,"1000",&signature(raw),1000.0,262144).unwrap(), WebhookEvent::Challenge { challenge, .. } if challenge == "confirm")
    );
    assert!(webhook.verify(raw, "1000", &signature(raw), 1300.0).is_ok());
    assert_eq!(
        webhook.verify(raw, "1000", &signature(raw), 1300.1),
        Err(WebhookError::Signature)
    );
}
#[test]
fn webhook_authentication_size_and_syntax_errors_are_distinct() {
    let webhook = SlackWebhook::new("secret");
    let raw = b"invalid";
    assert!(matches!(
        webhook.event(raw, "1000", "bad", 1000.0, 100),
        Err(WebhookError::Signature)
    ));
    assert!(matches!(
        webhook.event(raw, "1000", &signature(raw), 1000.0, 100),
        Err(WebhookError::Payload)
    ));
    assert!(matches!(
        webhook.event(raw, "1000", &signature(raw), 1000.0, 1),
        Err(WebhookError::TooLarge)
    ));
    for raw in [
        br#"[]"#.as_slice(),
        br#"{"type":"event_callback","team_id":"T1","event_id":false,"event":{}}"#,
        br#"{"type":"url_verification","challenge":null}"#,
    ] {
        assert!(matches!(
            webhook.event(raw, "1000", &signature(raw), 1000.0, 1000),
            Err(WebhookError::Payload)
        ));
    }
}
#[test]
fn interaction_parses_one_payload_and_rejects_ambiguous_or_invalid_encodings() {
    let webhook = SlackWebhook::new("secret");
    let raw = b"payload=%7B%22type%22%3A%22block_actions%22%7D";
    assert_eq!(
        webhook
            .interaction(raw, "1000", &signature(raw), 1000.0, 65536)
            .unwrap()["type"],
        "block_actions"
    );
    for raw in [
        b"payload=%7B%7D&payload=%7B%7D".as_slice(),
        b"payload=%GG",
        b"payload=%7B%22x%22%3A%22%FF%22%7D",
        b"payload=%5B%5D",
        b"payload=%7B%7D&bad",
    ] {
        assert!(matches!(
            webhook.interaction(raw, "1000", &signature(raw), 1000.0, 65536),
            Err(WebhookError::Payload)
        ));
    }
}
fn request() -> MessageRequest {
    MessageRequest::new(
        &RichReply {
            text: "Ready".into(),
            blocks: vec![],
            attachments: vec![],
        },
        "C123",
        Some("123.001"),
        None,
        None,
        false,
    )
    .unwrap()
}
#[test]
fn receipts_require_positive_evidence_and_valid_routing() {
    let request = request();
    assert_eq!(
        DeliveryResult::from_response(&request, json!({"ok":true,"ts":"123.002"})).status,
        DeliveryStatus::Confirmed
    );
    assert_eq!(
        DeliveryResult::from_response(&request, json!({"ok":false,"error":"missing_scope"})).status,
        DeliveryStatus::Rejected
    );
    for response in [
        json!({"ok":true}),
        json!({"ok":1,"ts":"123.002"}),
        json!({"ok":true,"ts":"invalid"}),
        json!({"ok":true,"ts":"123.002","channel":"COTHER"}),
        json!([]),
    ] {
        assert_eq!(
            DeliveryResult::from_response(&request, response).status,
            DeliveryStatus::Uncertain
        );
    }
    assert_eq!(
        serde_json::to_value(prepare_request(&serde_json::to_value(&request).unwrap()).unwrap())
            .unwrap(),
        serde_json::to_value(request).unwrap()
    );
    assert!(prepare_request(&json!({"method":"other","payload":{}})).is_err());
}
fn message(kind: &str, identity: &str, thread: &str) -> serde_json::Value {
    json!({"ts":"123.002","thread_ts":thread,"metadata":{"event_type":kind,"event_payload":{"id":identity}}})
}
#[test]
fn reconciliation_matches_type_id_and_thread_then_follows_cursor() {
    let mut reader = Reconciliation::new("C123", "123.001", "delivery", "one", 5).unwrap();
    assert!(!reader.accept(&json!({"ok":true,"messages":[message("other","one","123.001"),message("delivery","other","123.001"),message("delivery","one","999.001")],"response_metadata":{"next_cursor":"second"}})));
    assert_eq!(reader.request()["cursor"], "second");
    assert!(reader.accept(&json!({"ok":true,"messages":[message("delivery","one","123.001")]})));
    assert_eq!(reader.finish().status, DeliveryStatus::Confirmed);
}
#[test]
fn reconciliation_never_treats_absence_limits_or_provider_failure_as_failed_write() {
    for (response, reason) in [
        (json!({"ok":true,"messages":[]}), "not_found"),
        (
            json!({"ok":false,"error":"missing_scope"}),
            "history_unavailable",
        ),
        (
            json!({"ok":true,"messages":[],"has_more":true}),
            "missing_cursor",
        ),
        (
            json!({"ok":true,"messages":[],"response_metadata":{"next_cursor":"next"}}),
            "page_limit",
        ),
    ] {
        let mut reader = Reconciliation::new("C123", "123.001", "delivery", "one", 1).unwrap();
        assert!(reader.accept(&response));
        let result = reader.finish();
        assert_eq!(result.status, DeliveryStatus::Uncertain);
        assert_eq!(result.reason.as_deref(), Some(reason));
    }
    let mut reader = Reconciliation::new("C123", "123.001", "delivery", "one", 5).unwrap();
    let response = json!({"ok":true,"messages":[],"response_metadata":{"next_cursor":"repeat"}});
    assert!(!reader.accept(&response));
    assert!(reader.accept(&response));
    assert_eq!(reader.finish().reason.as_deref(), Some("repeated_cursor"));
}
