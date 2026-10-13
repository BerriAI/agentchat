mod support;

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;

use agentchat_core::{Error, Role, UploadFile};
use agentchat_slack::{events, rendering, Slack};
use axum::http::StatusCode;
use serde_json::json;
use tokio::sync::{mpsc, Notify};

use support::{envelope, event, MockSlack, Reply};

#[tokio::test]
async fn authenticates_accounts_and_separates_bot_and_app_credentials() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move { slack.run(|_| async { Ok(()) }).await }
    });
    mock.connected(1).await;
    assert!(slack.is_connected());
    assert_eq!(
        mock.calls("auth.test").await[0].authorization.as_deref(),
        Some("Bearer xoxb-test-secret")
    );
    assert_eq!(
        mock.calls("apps.connections.open").await[0]
            .authorization
            .as_deref(),
        Some("Bearer xapp-test-secret")
    );
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
    assert!(!slack.is_connected());
    assert!(slack.run(|_| async { Ok(()) }).await.is_err());
}

#[tokio::test]
async fn refuses_a_different_workspace_before_opening_socket() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.workspace_id = Some("T2".into());
    let slack = Slack::new(config).unwrap();
    assert!(slack
        .run(|_| async { Ok(()) })
        .await
        .unwrap_err()
        .to_string()
        .contains("different workspace"));
    assert!(mock.calls("apps.connections.open").await.is_empty());
    slack.close().await.unwrap();
}

#[tokio::test]
async fn acknowledges_before_handler_and_leaves_overflow_for_retry() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.max_pending_events = 1;
    let slack = Slack::new(config).unwrap();
    let release = Arc::new(Notify::new());
    let (started, mut received) = mpsc::unbounded_channel();
    let run = tokio::spawn({
        let slack = slack.clone();
        let release = release.clone();
        async move {
            slack
                .run(move |message| {
                    let release = release.clone();
                    let started = started.clone();
                    async move {
                        started.send(message.id.to_string()).unwrap();
                        release.notified().await;
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    assert_eq!(mock.ack().await["envelope_id"], "one");
    assert_eq!(received.recv().await.unwrap(), "slack:T1:C1:1.000001");
    mock.send(envelope("two", "2.000001"));
    assert!(tokio::time::timeout(Duration::from_millis(50), mock.ack())
        .await
        .is_err());
    assert!(received.try_recv().is_err());
    release.notify_one();
    tokio::time::sleep(Duration::from_millis(20)).await;
    mock.send(envelope("two-retry", "2.000001"));
    assert_eq!(mock.ack().await["envelope_id"], "two-retry");
    assert_eq!(received.recv().await.unwrap(), "slack:T1:C1:2.000001");
    release.notify_one();
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
}

#[tokio::test]
async fn shutdown_drains_accepted_handler_and_allows_its_final_reply() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.shutdown_timeout = Duration::from_secs(2);
    let slack = Slack::new(config).unwrap();
    let release = Arc::new(Notify::new());
    let started = Arc::new(Notify::new());
    let run = tokio::spawn({
        let slack = slack.clone();
        let callback_slack = slack.clone();
        let release = release.clone();
        let started = started.clone();
        async move {
            slack
                .run(move |message| {
                    let slack = callback_slack.clone();
                    let release = release.clone();
                    let started = started.clone();
                    async move {
                        started.notify_one();
                        release.notified().await;
                        slack.reply(&message, "done").await?;
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    mock.ack().await;
    started.notified().await;
    let close = tokio::spawn({
        let slack = slack.clone();
        async move { slack.close().await }
    });
    tokio::task::yield_now().await;
    assert!(!close.is_finished());
    release.notify_one();
    tokio::time::timeout(Duration::from_secs(3), close)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    run.await.unwrap().unwrap();
    assert_eq!(mock.calls("chat.postMessage").await.len(), 1);
    slack.close().await.unwrap();
}

struct Dropped(Arc<AtomicBool>);
impl Drop for Dropped {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

#[tokio::test]
async fn shutdown_cancels_stuck_handlers_at_deadline() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.shutdown_timeout = Duration::from_millis(20);
    let slack = Slack::new(config).unwrap();
    let dropped = Arc::new(AtomicBool::new(false));
    let started = Arc::new(Notify::new());
    let run = tokio::spawn({
        let slack = slack.clone();
        let dropped = dropped.clone();
        let started = started.clone();
        async move {
            slack
                .run(move |_| {
                    let dropped = dropped.clone();
                    let started = started.clone();
                    async move {
                        let _guard = Dropped(dropped);
                        started.notify_one();
                        std::future::pending::<()>().await;
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    started.notified().await;
    tokio::time::timeout(Duration::from_secs(1), slack.close())
        .await
        .unwrap()
        .unwrap();
    run.await.unwrap().unwrap();
    assert!(dropped.load(Ordering::Acquire));
}

#[tokio::test]
async fn cancelled_run_stops_ingress_and_cleans_up() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move { slack.run(|_| async { Ok(()) }).await }
    });
    mock.connected(1).await;
    run.abort();
    assert!(run.await.unwrap_err().is_cancelled());
    tokio::time::timeout(Duration::from_secs(1), slack.close())
        .await
        .unwrap()
        .unwrap();
    assert!(!slack.is_connected());
}

#[tokio::test]
async fn close_cancels_inflight_authentication_without_waiting_for_http_timeout() {
    let mock = MockSlack::start().await;
    let gate = Arc::new(Notify::new());
    mock.override_reply(
        "auth.test",
        Reply::Gate(
            gate.clone(),
            json!({"ok":true,"team_id":"T1","user_id":"BOT"}),
        ),
    )
    .await;
    let slack = mock.slack();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move { slack.run(|_| async { Ok(()) }).await }
    });
    tokio::time::timeout(Duration::from_secs(2), async {
        while mock.calls("auth.test").await.is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    tokio::time::timeout(Duration::from_secs(1), slack.close())
        .await
        .unwrap()
        .unwrap();
    run.await.unwrap().unwrap();
    assert!(mock.calls("apps.connections.open").await.is_empty());
    gate.notify_one();
}

#[tokio::test]
async fn reconnects_with_a_fresh_socket_url() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    let (sent, mut received) = mpsc::unbounded_channel();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move {
            slack
                .run(move |message| {
                    let sent = sent.clone();
                    async move {
                        sent.send(message).unwrap();
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(json!({"type":"disconnect","reason":"refresh_requested"}));
    mock.connected(2).await;
    mock.send(envelope("after-reconnect", "1.000001"));
    assert_eq!(mock.ack().await["envelope_id"], "after-reconnect");
    assert_eq!(received.recv().await.unwrap().text, "hello");
    assert_eq!(mock.calls("apps.connections.open").await.len(), 2);
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
}

#[tokio::test]
async fn receiver_errors_propagate_instead_of_disappearing_in_background_tasks() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.stop_on_handler_error = true;
    let slack = Slack::new(config).unwrap();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move {
            slack
                .run(|_| async { Err(Error::Handler("agent failed".into())) })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    let error = tokio::time::timeout(Duration::from_secs(2), run)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert_eq!(error.to_string(), "agent failed");
    slack.close().await.unwrap();
}

#[tokio::test]
async fn default_handler_failure_policy_keeps_other_turns_running() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    let (sent, mut received) = mpsc::unbounded_channel();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move {
            slack
                .run(move |message| {
                    let sent = sent.clone();
                    async move {
                        sent.send(message.id).unwrap();
                        Err(Error::Handler("agent failed".into()))
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    for (id, ts) in [("one", "1.000001"), ("two", "2.000001")] {
        mock.send(envelope(id, ts));
        assert_eq!(mock.ack().await["envelope_id"], id);
        received.recv().await.unwrap();
    }
    assert!(!run.is_finished());
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
}

#[tokio::test]
async fn a_handler_can_close_without_waiting_for_its_own_drain() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    let finished = Arc::new(AtomicBool::new(false));
    let run = tokio::spawn({
        let slack = slack.clone();
        let callback_slack = slack.clone();
        let finished = finished.clone();
        async move {
            slack
                .run(move |message| {
                    let slack = callback_slack.clone();
                    let finished = finished.clone();
                    async move {
                        slack.close().await?;
                        slack.reply(&message, "goodbye").await?;
                        finished.store(true, Ordering::Release);
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    tokio::time::timeout(Duration::from_secs(2), run)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(finished.load(Ordering::Acquire));
    assert_eq!(mock.calls("chat.postMessage").await.len(), 1);
}

#[tokio::test]
async fn closing_allows_original_admitted_ffi_context_but_rejects_new_or_altered_sources() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.shutdown_timeout = Duration::from_secs(2);
    let slack = Slack::new(config).unwrap();
    let release = Arc::new(Notify::new());
    let (sent, mut received) = mpsc::unbounded_channel();
    let run = tokio::spawn({
        let slack = slack.clone();
        let release = release.clone();
        async move {
            slack
                .run(move |message| {
                    let release = release.clone();
                    let sent = sent.clone();
                    async move {
                        sent.send(message).unwrap();
                        release.notified().await;
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    let source = received.recv().await.unwrap();
    slack.request_close();
    assert!(matches!(
        slack.reply(&source, "unscoped").await,
        Err(Error::Closed)
    ));
    let mut altered = source.clone();
    altered.metadata.insert("channel_id".into(), json!("C2"));
    assert!(matches!(
        slack
            .with_admitted_source(&altered, slack.reply(&altered, "altered"))
            .await,
        Err(Error::Closed)
    ));
    slack
        .with_admitted_source(&source, slack.reply(&source, "allowed"))
        .await
        .unwrap();
    release.notify_one();
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
    assert_eq!(mock.calls("chat.postMessage").await.len(), 1);
    assert!(slack
        .with_admitted_source(&source, slack.reply(&source, "late"))
        .await
        .is_err());
}

#[tokio::test]
async fn forced_shutdown_cancels_native_operations_started_on_another_ffi_task() {
    let mock = MockSlack::start().await;
    let mut config = mock.config();
    config.shutdown_timeout = Duration::from_millis(20);
    let slack = Slack::new(config).unwrap();
    let (sent, mut received) = mpsc::unbounded_channel();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move {
            slack
                .run(move |message| {
                    let sent = sent.clone();
                    async move {
                        sent.send(message).unwrap();
                        std::future::pending::<()>().await;
                        Ok(())
                    }
                })
                .await
        }
    });
    mock.connected(1).await;
    mock.send(envelope("one", "1.000001"));
    let source = received.recv().await.unwrap();
    let started = Arc::new(Notify::new());
    let dropped = Arc::new(AtomicBool::new(false));
    let operation = tokio::spawn({
        let slack = slack.clone();
        let started = started.clone();
        let dropped = dropped.clone();
        async move {
            slack
                .with_admitted_source(&source, async {
                    let _drop = Dropped(dropped);
                    started.notify_one();
                    std::future::pending::<agentchat_core::Result<()>>().await
                })
                .await
        }
    });
    started.notified().await;
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
    let error = tokio::time::timeout(Duration::from_secs(1), operation)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert!(error.to_string().contains("delivery may be uncertain"));
    assert!(dropped.load(Ordering::Acquire));
}

#[tokio::test]
async fn subscription_and_route_checks_run_before_network_sends() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    slack.authenticate().await.unwrap();
    let root = events::normalize(&event("1.000001"), "BOT", "T1").unwrap();
    let mut payload = event("2.000001");
    payload["event"]["type"] = json!("message");
    payload["event"]["channel_type"] = json!("channel");
    payload["event"]["thread_ts"] = json!("1.000001");
    payload["event"]["text"] = json!("follow up");
    assert!(slack.handle_event(&payload).await.unwrap().is_none());
    slack.subscribe(&root).await.unwrap();
    assert!(slack.handle_event(&payload).await.unwrap().is_some());
    let mut foreign = root.clone();
    foreign.metadata.insert("team_id".into(), json!("T2"));
    assert!(slack.reply(&foreign, "no").await.is_err());
    assert!(slack.set_status(&foreign, "working").await.is_err());
    assert!(mock.calls("chat.postMessage").await.is_empty());
    let sent = slack.reply(&root, "answer 🦀").await.unwrap();
    assert_eq!(sent.id.as_ref(), "slack:C1:9.000001");
    assert_eq!(sent.conversation_id, root.conversation_id);
    assert_eq!(sent.role, Role::Assistant);
    assert_eq!(
        mock.calls("chat.postMessage").await[0].payload["thread_ts"],
        "1.000001"
    );
}

#[tokio::test]
async fn sends_require_receipts_and_never_retry_errors_or_redirects() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    slack.authenticate().await.unwrap();
    let source = events::normalize(&event("1.000001"), "BOT", "T1").unwrap();
    for reply in [
        Reply::Json(StatusCode::INTERNAL_SERVER_ERROR, json!({"ok":false})),
        Reply::Json(StatusCode::OK, json!({"ok":true})),
        Reply::Json(StatusCode::OK, json!({"ok":true,"ts":"unknown"})),
        Reply::Redirect(format!("{}/api/should-not-follow", mock.state.base)),
    ] {
        let before = mock.calls("chat.postMessage").await.len();
        mock.override_reply("chat.postMessage", reply).await;
        assert!(slack.reply(&source, "answer").await.is_err());
        assert_eq!(mock.calls("chat.postMessage").await.len(), before + 1);
    }
    assert!(mock.calls("should-not-follow").await.is_empty());
}

#[tokio::test]
async fn status_does_not_create_threads_in_plain_dms() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    slack.authenticate().await.unwrap();
    let mut payload = event("1.000001");
    payload["event"]["type"] = json!("message");
    payload["event"]["channel_type"] = json!("im");
    let dm = events::normalize(&payload, "BOT", "T1").unwrap();
    assert!(!slack.set_status(&dm, "working").await.unwrap());
    assert!(mock.calls("assistant.threads.setStatus").await.is_empty());
    let thread = events::normalize(&event("1.000001"), "BOT", "T1").unwrap();
    assert!(slack.set_status(&thread, "working").await.unwrap());
}

#[tokio::test]
async fn stop_admission_leaves_existing_status_cleanup_available_until_close() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    let run = tokio::spawn({
        let slack = slack.clone();
        async move {
            slack
                .run(|_| async { panic!("must not admit after stop_admission") })
                .await
        }
    });
    mock.connected(1).await;
    slack.stop_admission();
    mock.send(envelope("unadmitted", "1.000001"));
    assert!(tokio::time::timeout(Duration::from_millis(50), mock.ack())
        .await
        .is_err());
    let source = events::normalize(&event("1.000001"), "BOT", "T1").unwrap();
    assert!(slack.handle_event(&event("1.000001")).await.is_err());
    assert!(slack.set_status(&source, "").await.unwrap());
    assert_eq!(mock.calls("assistant.threads.setStatus").await.len(), 1);
    slack.close().await.unwrap();
    run.await.unwrap().unwrap();
    assert!(slack.set_status(&source, "").await.is_err());
}

#[tokio::test]
async fn history_preserves_root_recent_messages_and_authenticates_mirror_metadata() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    slack.authenticate().await.unwrap();
    let mut payload = event("3.000001");
    payload["event"]["thread_ts"] = json!("1.000001");
    let source = events::normalize(&payload, "BOT", "T1").unwrap();
    let mut mirror = rendering::mirror_payload(&source, "web").unwrap();
    mirror["user"] = json!("BOT");
    mirror["ts"] = json!("2.000001");
    mock.override_reply(
        "conversations.replies",
        Reply::Json(
            StatusCode::OK,
            json!({"ok":true,
        "messages":[{"ts":"1.000001","user":"U0","text":"root"}, mirror,
            {"ts":"3.000001","user":"U1","text":"current"}],"has_more":false}),
        ),
    )
    .await;
    let history = slack.thread_history(&source, 2).await.unwrap();
    assert_eq!(history.len(), 2);
    assert_eq!(history[0].text, "root");
    assert_eq!(history[1].sender.id, "U1");
    assert_eq!(history[1].role, Role::User);
    assert_eq!(history[1].text, "hello");
    assert!(mock.calls("conversations.replies").await[0]
        .query
        .as_ref()
        .unwrap()
        .contains("inclusive=false"));
}

#[tokio::test]
async fn file_upload_is_staged_and_download_is_explicit_authenticated_and_bounded() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    slack.authenticate().await.unwrap();
    let mut payload = event("1.000001");
    payload["event"]["files"] = json!([{"id":"F1"}]);
    let source = events::normalize(&payload, "BOT", "T1").unwrap();
    assert!(mock.calls("files.info").await.is_empty());
    let uploaded = slack
        .upload_files(
            &source,
            &[UploadFile {
                filename: "test.txt".into(),
                content: b"test".to_vec(),
                title: None,
            }],
        )
        .await
        .unwrap();
    assert_eq!(uploaded[0].id, "F1");
    assert_eq!(mock.calls("upload").await[0].authorization, None);
    assert_eq!(
        mock.calls("files.completeUploadExternal").await[0].payload["thread_ts"],
        "1.000001"
    );
    let downloaded = slack
        .download_attachment(&source, &source.attachments[0], 4)
        .await
        .unwrap();
    assert_eq!(downloaded.content, b"test");
    assert_eq!(
        mock.calls("download").await[0].authorization.as_deref(),
        Some("Bearer xoxb-test-secret")
    );
    mock.override_reply("download", Reply::Bytes(b"too long".to_vec()))
        .await;
    assert!(slack
        .download_attachment(&source, &source.attachments[0], 4)
        .await
        .is_err());
    let mut unauthorized = source.attachments[0].clone();
    unauthorized.id = "F2".into();
    assert!(slack
        .download_attachment(&source, &unauthorized, 100)
        .await
        .is_err());
}

#[tokio::test]
async fn files_reject_untrusted_destinations_before_sending_credentials() {
    let mock = MockSlack::start().await;
    let slack = mock.slack();
    slack.authenticate().await.unwrap();
    let mut payload = event("1.000001");
    payload["event"]["files"] = json!([{"id":"F1"}]);
    let source = events::normalize(&payload, "BOT", "T1").unwrap();
    mock.override_reply(
        "files.info",
        Reply::Json(
            StatusCode::OK,
            json!({"ok":true,"file":{
        "id":"F1","size":1,"url_private_download":"https://example.com/files-pri/F1"}}),
        ),
    )
    .await;
    assert!(slack
        .download_attachment(&source, &source.attachments[0], 10)
        .await
        .is_err());
    assert!(mock.calls("download").await.is_empty());
    mock.override_reply(
        "files.getUploadURLExternal",
        Reply::Json(
            StatusCode::OK,
            json!({"ok":true,
        "file_id":"F1","upload_url":"https://files.slack.com.evil.example/upload/F1"}),
        ),
    )
    .await;
    assert!(slack
        .upload_files(
            &source,
            &[UploadFile {
                filename: "x".into(),
                content: vec![1],
                title: None
            }]
        )
        .await
        .is_err());
    assert!(mock.calls("files.completeUploadExternal").await.is_empty());
}

#[test]
fn configuration_debug_redacts_credentials_and_rejects_remote_plaintext() {
    let config = agentchat_slack::SlackConfig::new("secret-bot".into(), "secret-app".into());
    let debug = format!("{config:?}");
    assert!(!debug.contains("secret"));
    let mut unsafe_config = config;
    unsafe_config.api_base_url = "http://example.com/api".into();
    assert!(Slack::new(unsafe_config).is_err());
}
