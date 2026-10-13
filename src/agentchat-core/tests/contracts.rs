use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use std::time::Duration;

use agentchat_core::*;
use async_trait::async_trait;
use tokio::sync::{Mutex, Semaphore};

fn incoming(id: &str, conversation: &str) -> Message {
    Message {
        id: id.into(),
        conversation_id: conversation.into(),
        channel: "fake".into(),
        sender: Sender {
            id: "alice".into(),
            display_name: None,
        },
        text: id.into(),
        role: Role::User,
        metadata: Default::default(),
        thread_id: None,
        addressed: true,
        attachments: vec![],
    }
}

#[derive(Default)]
struct FakeChannel {
    calls: AtomicUsize,
    sent: Mutex<Vec<String>>,
    uncertain: bool,
    history: Option<Vec<Message>>,
    history_fails: bool,
    statuses: Mutex<Vec<String>>,
    status_supported: bool,
    status_fails: bool,
}

#[async_trait]
impl Channel for FakeChannel {
    fn name(&self) -> &str {
        "fake"
    }

    async fn set_status(&self, _source: &Message, status: &str) -> Result<bool> {
        if !self.status_supported {
            return Ok(false);
        }
        self.statuses.lock().await.push(status.to_owned());
        if self.status_fails {
            return Err(Error::Transport("Status unavailable".into()));
        }
        Ok(true)
    }

    async fn reply(&self, source: &Message, content: &str) -> Result<Message> {
        let sequence = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        self.sent.lock().await.push(content.to_owned());
        if self.uncertain {
            return Err(Error::Transport("Delivery outcome is unknown".into()));
        }
        Ok(Message {
            id: format!("reply:{sequence}").into(),
            sender: Sender {
                id: "bot".into(),
                display_name: None,
            },
            text: content.into(),
            role: Role::Assistant,
            ..source.clone()
        })
    }

    async fn thread_history(
        &self,
        _source: &Message,
        _limit: usize,
    ) -> Result<Option<Vec<Message>>> {
        if self.history_fails {
            return Err(Error::Transport("History unavailable".into()));
        }
        Ok(self.history.clone())
    }
}

#[test]
fn typed_turn_enforces_each_effect_and_keeps_empty_replies() {
    let mut turn = Turn::new();
    assert_eq!(turn.effect(), &TurnEffect::Claim);
    assert!(turn.resolve(TurnResult::ReplySent).is_err());
    assert_eq!(turn.effect(), &TurnEffect::Claim);
    turn.resolve(TurnResult::Claimed(true)).unwrap();
    assert_eq!(turn.effect(), &TurnEffect::AcquireConversation);
    turn.resolve(TurnResult::ConversationAcquired).unwrap();
    turn.resolve(TurnResult::IncomingAppended).unwrap();
    turn.resolve(TurnResult::HandlerCompleted(Some(String::new())))
        .unwrap();
    assert_eq!(turn.effect(), &TurnEffect::SendReply(String::new()));
    turn.resolve(TurnResult::ReplySent).unwrap();
    turn.resolve(TurnResult::ReplyAppended).unwrap();
    assert_eq!(turn.effect(), &TurnEffect::Complete);
    assert!(turn.resolve(TurnResult::Claimed(true)).is_err());
}

#[test]
fn duplicate_finishes_without_any_other_effect() {
    let mut turn = Turn::new();
    turn.resolve(TurnResult::Claimed(false)).unwrap();
    assert_eq!(turn.effect(), &TurnEffect::Complete);
}

#[test]
fn history_selection_preserves_root_and_current_turn() {
    assert_eq!(thread_history_indices(5, 3).unwrap(), vec![0, 3, 4]);
    assert_eq!(thread_history_indices(1, 3).unwrap(), vec![0]);
    assert!(thread_history_indices(5, 1).is_err());
    assert!(thread_history_indices(5, 101).is_err());
}

#[test]
fn memory_values_preserve_identity_and_zero_limit_matches_python() {
    let mut store = MemoryStore::new();
    let value = Arc::new(vec![1, 2, 3]);
    let conversation = ConversationId::from("example");
    assert!(store.claim(&MessageId::from("one")));
    assert!(!store.claim(&MessageId::from("one")));
    store.append(&conversation, value.clone());
    assert!(Arc::ptr_eq(
        &store.history(&conversation, Some(0))[0],
        &value
    ));
    assert!(store
        .history(&ConversationId::from("missing"), None)
        .is_empty());
}

#[test]
fn portable_ids_are_strings_and_optional_message_fields_have_defaults() {
    let message: Message = serde_json::from_value(serde_json::json!({
        "id": "discord:999999999999999999", "conversation_id": "slack:T:C:1.000001",
        "channel": "custom", "sender": {"id": "person"}, "text": "hello", "role": "user"
    }))
    .unwrap();
    assert!(message.addressed);
    assert_eq!(message.id.as_ref(), "discord:999999999999999999");
    let value = serde_json::to_value(&message).unwrap();
    assert_eq!(value["id"], "discord:999999999999999999");
    assert_eq!(value["role"], "user");
}

#[test]
fn file_debug_output_does_not_include_bytes() {
    let content = b"private attachment".to_vec();
    let upload = UploadFile {
        filename: "report.txt".into(),
        content: content.clone(),
        title: None,
    };
    let download = DownloadedFile {
        id: "file".into(),
        name: "report.txt".into(),
        content: content.clone(),
        media_type: None,
    };
    for debug in [format!("{upload:?}"), format!("{download:?}")] {
        assert!(debug.contains("content_length: 18"));
        assert!(!debug.contains("private attachment"));
        assert!(!debug.contains(&format!("{content:?}")));
    }
}

#[tokio::test]
async fn nested_reply_and_history_do_not_reacquire_the_conversation_lock() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel::default());
    tokio::time::timeout(
        Duration::from_secs(1),
        app.receive(
            channel.clone(),
            incoming("one", "chat"),
            |context| async move {
                assert_eq!(context.history(30).await?.len(), 1);
                context.reply("manual reply").await?;
                assert_eq!(context.conversation.history(None).await?.len(), 2);
                Ok(None)
            },
        ),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(channel.calls.load(Ordering::SeqCst), 1);
    assert_eq!(app.history(&"chat".into(), None).await.unwrap().len(), 2);
}

#[tokio::test]
async fn uncertain_send_never_appends_success_or_retries_claimed_input() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel {
        uncertain: true,
        ..Default::default()
    });
    let message = incoming("one", "chat");
    let result = app
        .receive(channel.clone(), message.clone(), |_| async {
            Ok(Some("answer".into()))
        })
        .await;
    assert!(matches!(result, Err(Error::Transport(_))));
    app.receive(channel.clone(), message, |_| async {
        panic!("a claimed message must not run again")
    })
    .await
    .unwrap();
    assert_eq!(channel.calls.load(Ordering::SeqCst), 1);
    assert_eq!(app.history(&"chat".into(), None).await.unwrap().len(), 1);
}

#[tokio::test]
async fn handler_failure_keeps_input_and_claim_and_releases_permit() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel::default());
    assert!(app
        .receive(channel.clone(), incoming("one", "chat"), |_| async {
            Err(Error::Handler("failed".into()))
        })
        .await
        .is_err());
    app.receive(channel.clone(), incoming("one", "chat"), |_| async {
        panic!("duplicate")
    })
    .await
    .unwrap();
    app.receive(channel, incoming("two", "chat"), |_| async { Ok(None) })
        .await
        .unwrap();
    assert_eq!(app.history(&"chat".into(), None).await.unwrap().len(), 2);
}

#[tokio::test]
async fn conversations_serialize_independently() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel::default());
    let started = Arc::new(Semaphore::new(0));
    let release = Arc::new(Semaphore::new(0));
    let app1 = app.clone();
    let channel1 = channel.clone();
    let started1 = started.clone();
    let release1 = release.clone();
    let first = tokio::spawn(async move {
        app1.receive(channel1, incoming("one", "chat"), |_| async move {
            started1.add_permits(1);
            release1.acquire().await.unwrap().forget();
            Ok(Some("first".into()))
        })
        .await
    });
    started.acquire().await.unwrap().forget();
    let app2 = app.clone();
    let channel2 = channel.clone();
    let second = tokio::spawn(async move {
        app2.receive(channel2, incoming("two", "chat"), |context| async move {
            assert_eq!(context.conversation.history(None).await?.len(), 3);
            Ok(Some("second".into()))
        })
        .await
    });
    tokio::time::timeout(
        Duration::from_secs(1),
        app.receive(channel.clone(), incoming("other", "separate"), |_| async {
            Ok(None)
        }),
    )
    .await
    .unwrap()
    .unwrap();
    release.add_permits(1);
    first.await.unwrap().unwrap();
    second.await.unwrap().unwrap();
    assert_eq!(*channel.sent.lock().await, vec!["first", "second"]);
}

#[tokio::test]
async fn cancellation_releases_lock_without_releasing_claim() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel::default());
    let started = Arc::new(Semaphore::new(0));
    let app1 = app.clone();
    let channel1 = channel.clone();
    let started1 = started.clone();
    let task = tokio::spawn(async move {
        app1.receive(channel1, incoming("cancelled", "chat"), |_| async move {
            started1.add_permits(1);
            std::future::pending::<Result<Option<String>>>().await
        })
        .await
    });
    started.acquire().await.unwrap().forget();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    app.receive(channel.clone(), incoming("cancelled", "chat"), |_| async {
        panic!("duplicate")
    })
    .await
    .unwrap();
    tokio::time::timeout(
        Duration::from_secs(1),
        app.receive(channel, incoming("next", "chat"), |_| async { Ok(None) }),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(app.history(&"chat".into(), None).await.unwrap().len(), 2);
}

#[tokio::test]
async fn native_history_keeps_root_and_tail_without_replaying_past_turns() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel {
        history: Some(vec![
            incoming("root", "chat"),
            incoming("middle", "chat"),
            incoming("last", "chat"),
        ]),
        ..Default::default()
    });
    let message = Message {
        thread_id: Some("thread".into()),
        ..incoming("current", "chat")
    };
    app.receive(channel, message, |context| async move {
        let history = context.history(3).await?;
        assert_eq!(
            history.iter().map(|m| m.id.as_ref()).collect::<Vec<_>>(),
            vec!["root", "last", "current"]
        );
        assert_eq!(context.conversation.history(None).await?.len(), 1);
        Ok(None)
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn native_history_failure_does_not_fall_back_to_local_history() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel {
        history_fails: true,
        ..Default::default()
    });
    let message = Message {
        thread_id: Some("thread".into()),
        ..incoming("current", "chat")
    };
    let result = app
        .receive(channel.clone(), message, |context| async move {
            context.history(30).await?;
            Ok(Some("must not send".into()))
        })
        .await;
    assert!(matches!(result, Err(Error::Transport(_))));
    assert_eq!(channel.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn optional_operations_fail_without_sending_or_inventing_history() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel::default());
    let message = incoming("one", "chat");
    assert!(!app
        .set_status(channel.clone(), &message, "working")
        .await
        .unwrap());
    assert!(matches!(
        app.upload_files(channel.clone(), &message, &[]).await,
        Err(Error::Unsupported("file uploads"))
    ));
    assert!(matches!(
        app.mirror(channel.clone(), &message, &message, "web").await,
        Err(Error::Unsupported("mirroring"))
    ));
    assert!(app
        .history(&message.conversation_id, None)
        .await
        .unwrap()
        .is_empty());
    assert_eq!(channel.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test(start_paused = true)]
async fn activity_refreshes_latest_label_and_explicit_close_clears_once() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel {
        status_supported: true,
        ..Default::default()
    });
    let context = MessageContext::new(app.clone(), channel.clone(), incoming("status", "chat"));
    let mut working = context
        .working("working", Duration::from_secs(10))
        .await
        .unwrap();
    assert!(working.update("finishing").await);
    tokio::task::yield_now().await;
    tokio::time::advance(Duration::from_secs(10)).await;
    tokio::task::yield_now().await;
    working.close().await;
    working.close().await;
    drop(working);
    assert_eq!(
        *channel.statuses.lock().await,
        vec!["working", "finishing", "finishing", ""]
    );
    assert!(app.history(&"chat".into(), None).await.unwrap().is_empty());
}

#[tokio::test]
async fn activity_transport_failure_does_not_prevent_an_answer() {
    let app = Arc::new(Runtime::new());
    let channel = Arc::new(FakeChannel {
        status_supported: true,
        status_fails: true,
        ..Default::default()
    });
    app.receive(
        channel.clone(),
        incoming("status", "chat"),
        |context| async move {
            let mut working = context.working("working", Duration::from_secs(60)).await?;
            assert!(!working.update("still working").await);
            working.close().await;
            Ok(Some("answer".into()))
        },
    )
    .await
    .unwrap();
    assert_eq!(channel.calls.load(Ordering::SeqCst), 1);
    assert_eq!(app.history(&"chat".into(), None).await.unwrap().len(), 2);
}

#[tokio::test]
async fn activity_drop_schedules_best_effort_clear() {
    let channel = Arc::new(FakeChannel {
        status_supported: true,
        ..Default::default()
    });
    let working = WorkingStatus::start(
        channel.clone(),
        incoming("status", "chat"),
        "working".into(),
        Duration::from_secs(60),
    )
    .await
    .unwrap();
    drop(working);
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            if channel
                .statuses
                .lock()
                .await
                .last()
                .is_some_and(String::is_empty)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(*channel.statuses.lock().await, vec!["working", ""]);
}
