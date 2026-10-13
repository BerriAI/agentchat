//! Native Slack transport shared by Rust and Node applications.
//!
//! Channel methods make one outbound attempt. A successful result includes a
//! provider receipt; a failed write may have an uncertain delivery outcome.
//! Socket acknowledgements accept bounded in-memory work, not a durable queue.

pub mod events;
mod files;
pub mod rendering;
mod route;
mod socket;
mod transport;

use std::collections::{HashMap, HashSet, VecDeque};
use std::fmt;
use std::future::Future;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering},
    Arc,
};
use std::time::Duration;

use agentchat_core::{
    Attachment, Channel, ConversationId, DownloadedFile, Error, HistoryLimit, Message, MessageId,
    Result, RichReply, Role, Sender, UploadFile, UploadedFile,
};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::{Mutex, RwLock};
use tokio_util::sync::CancellationToken;

use route::{Destination, SlackTimestamp};
use transport::WebClient;

const NEW: u8 = 0;
const RUNNING: u8 = 1;
const CLOSING: u8 = 2;
const CLOSED: u8 = 3;

tokio::task_local! { static DELIVERY_SCOPE: Arc<Inner>; }

#[derive(Clone)]
pub struct SlackConfig {
    pub bot_token: String,
    pub app_token: String,
    pub workspace_id: Option<String>,
    pub bot_user_id: Option<String>,
    pub ack_emoji: Option<String>,
    pub max_pending_events: usize,
    pub shutdown_timeout: Duration,
    /// Preserve the Python adapter's continue-on-handler-error behavior by default.
    pub stop_on_handler_error: bool,
    /// Override for a trusted API proxy or loopback test server. Never agent-controlled.
    pub api_base_url: String,
}

impl SlackConfig {
    pub fn new(bot_token: String, app_token: String) -> Self {
        Self {
            bot_token,
            app_token,
            workspace_id: None,
            bot_user_id: None,
            ack_emoji: Some("eyes".into()),
            max_pending_events: 64,
            shutdown_timeout: Duration::from_secs(20),
            stop_on_handler_error: false,
            api_base_url: "https://slack.com/api/".into(),
        }
    }

    pub fn from_env() -> Result<Self> {
        let bot_token = std::env::var("SLACK_BOT_TOKEN")
            .map_err(|_| Error::InvalidInput("SLACK_BOT_TOKEN is required".into()))?;
        let app_token = std::env::var("SLACK_APP_TOKEN")
            .map_err(|_| Error::InvalidInput("SLACK_APP_TOKEN is required".into()))?;
        Ok(Self::new(bot_token, app_token))
    }
}

impl fmt::Debug for SlackConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SlackConfig")
            .field("bot_token", &"[REDACTED]")
            .field("app_token", &"[REDACTED]")
            .field("workspace_id", &self.workspace_id)
            .field("max_pending_events", &self.max_pending_events)
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SlackIdentity {
    pub workspace_id: String,
    pub bot_user_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SlackUser {
    pub id: String,
    pub team_id: String,
    pub display_name: Option<String>,
    pub email: Option<String>,
    pub is_bot: bool,
    pub deleted: bool,
}

/// Applications may persist or expire subscriptions in their own store.
#[async_trait]
pub trait ThreadSubscriptions: Send + Sync {
    async fn contains(&self, conversation_id: &str) -> Result<bool>;
    async fn add(&self, conversation_id: &str) -> Result<()>;
}

#[derive(Default)]
pub struct MemoryThreadSubscriptions {
    threads: Mutex<VecDeque<String>>,
}

#[async_trait]
impl ThreadSubscriptions for MemoryThreadSubscriptions {
    async fn contains(&self, conversation_id: &str) -> Result<bool> {
        Ok(self
            .threads
            .lock()
            .await
            .iter()
            .any(|id| id == conversation_id))
    }
    async fn add(&self, conversation_id: &str) -> Result<()> {
        let mut threads = self.threads.lock().await;
        threads.retain(|id| id != conversation_id);
        threads.push_back(conversation_id.into());
        if threads.len() > 1000 {
            threads.pop_front();
        }
        Ok(())
    }
}

struct Admission {
    id: u64,
    source: Message,
    lifetime: CancellationToken,
}

struct Inner {
    config: SlackConfig,
    client: WebClient,
    identity: RwLock<Option<SlackIdentity>>,
    authentication: Mutex<()>,
    subscriptions: Arc<dyn ThreadSubscriptions>,
    lifecycle: AtomicU8,
    connected: AtomicBool,
    accepting: AtomicBool,
    stop: CancellationToken,
    done: CancellationToken,
    admitted: std::sync::Mutex<HashMap<(MessageId, ConversationId), Vec<Admission>>>,
    next_admission: AtomicU64,
}

#[derive(Clone)]
pub struct Slack {
    inner: Arc<Inner>,
}

impl Slack {
    pub fn new(config: SlackConfig) -> Result<Self> {
        Self::with_thread_subscriptions(config, Arc::new(MemoryThreadSubscriptions::default()))
    }

    pub fn from_env() -> Result<Self> {
        Self::new(SlackConfig::from_env()?)
    }

    pub fn with_thread_subscriptions(
        config: SlackConfig,
        subscriptions: Arc<dyn ThreadSubscriptions>,
    ) -> Result<Self> {
        if config.bot_token.trim().is_empty() || config.app_token.trim().is_empty() {
            return Err(Error::InvalidInput(
                "Slack bot_token and app_token are required".into(),
            ));
        }
        if config.max_pending_events == 0 {
            return Err(Error::InvalidInput(
                "max_pending_events must be positive".into(),
            ));
        }
        let client = WebClient::new(
            &config.api_base_url,
            config.bot_token.clone(),
            config.app_token.clone(),
        )?;
        Ok(Self {
            inner: Arc::new(Inner {
                config,
                client,
                identity: RwLock::new(None),
                authentication: Mutex::new(()),
                subscriptions,
                lifecycle: AtomicU8::new(NEW),
                connected: AtomicBool::new(false),
                accepting: AtomicBool::new(true),
                stop: CancellationToken::new(),
                done: CancellationToken::new(),
                admitted: std::sync::Mutex::new(HashMap::new()),
                next_admission: AtomicU64::new(0),
            }),
        })
    }

    /// Authenticate before accepting normalized events, including with host-owned ingress.
    pub async fn authenticate(&self) -> Result<SlackIdentity> {
        self.check_open()?;
        let _guard = self.inner.authentication.lock().await;
        if let Some(identity) = self.inner.identity.read().await.clone() {
            return Ok(identity);
        }
        let response = self.inner.client.call("auth.test", &json!({})).await?;
        let workspace_id = events::nonempty(&response["team_id"])
            .ok_or_else(|| Error::Transport("Slack did not return a workspace identity".into()))?;
        let bot_user_id = events::nonempty(&response["user_id"])
            .ok_or_else(|| Error::Transport("Slack did not return a bot identity".into()))?;
        if self
            .inner
            .config
            .workspace_id
            .as_ref()
            .is_some_and(|id| id != workspace_id)
            || self
                .inner
                .config
                .bot_user_id
                .as_ref()
                .is_some_and(|id| id != bot_user_id)
        {
            return Err(Error::InvalidInput(
                "Slack token belongs to a different workspace or bot".into(),
            ));
        }
        self.check_open()?;
        let identity = SlackIdentity {
            workspace_id: workspace_id.into(),
            bot_user_id: bot_user_id.into(),
        };
        *self.inner.identity.write().await = Some(identity.clone());
        Ok(identity)
    }

    pub fn is_connected(&self) -> bool {
        self.inner.connected.load(Ordering::Acquire) && !self.inner.stop.is_cancelled()
    }

    fn check_open(&self) -> Result<()> {
        let state = self.inner.lifecycle.load(Ordering::Acquire);
        if state == CLOSED || (state == CLOSING && !self.in_delivery()) {
            Err(Error::Closed)
        } else {
            Ok(())
        }
    }

    fn in_delivery(&self) -> bool {
        DELIVERY_SCOPE
            .try_with(|inner| Arc::ptr_eq(inner, &self.inner))
            .unwrap_or(false)
    }

    /// Restore an admitted receiver's operation scope across an FFI task boundary.
    /// Outside a live receiver, normal lifecycle checks still apply.
    pub async fn with_admitted_source<T, F>(&self, source: &Message, operation: F) -> Result<T>
    where
        F: Future<Output = Result<T>>,
    {
        let lifetime = self
            .inner
            .admitted
            .lock()
            .map_err(|_| Error::Closed)?
            .get(&(source.id.clone(), source.conversation_id.clone()))
            .and_then(|entries| entries.iter().find(|entry| entry.source == *source))
            .map(|entry| entry.lifetime.clone());
        if let Some(lifetime) = lifetime {
            DELIVERY_SCOPE.scope(self.inner.clone(), async {
                tokio::select! {
                    biased;
                    _ = lifetime.cancelled() => Err(Error::Transport("Slack operation was cancelled; delivery may be uncertain".into())),
                    result = operation => result,
                }
            }).await
        } else {
            self.check_open()?;
            operation.await
        }
    }

    async fn identity(&self) -> Result<SlackIdentity> {
        self.check_open()?;
        self.inner.identity.read().await.clone().ok_or_else(|| {
            Error::InvalidInput("Authenticate Slack before using this operation".into())
        })
    }

    async fn destination(&self, source: &Message) -> Result<Destination> {
        Destination::from_message(source, &self.identity().await?.workspace_id)
    }

    /// The caller must authenticate HTTP requests before using this host-ingress hook.
    pub async fn handle_event(&self, payload: &Value) -> Result<Option<Message>> {
        if self.inner.stop.is_cancelled() || !self.inner.accepting.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        self.prepare_event(payload).await
    }

    async fn prepare_event(&self, payload: &Value) -> Result<Option<Message>> {
        let identity = self.identity().await?;
        let Some(message) =
            events::normalize(payload, &identity.bot_user_id, &identity.workspace_id)
        else {
            return Ok(None);
        };
        if message.metadata.get("requires_subscription") == Some(&Value::Bool(true))
            && !self
                .inner
                .subscriptions
                .contains(message.conversation_id.as_ref())
                .await?
        {
            return Ok(None);
        }
        Ok(Some(message))
    }

    /// Subscribe only after the application authorizes the sender and conversation.
    pub async fn subscribe(&self, source: &Message) -> Result<()> {
        self.destination(source).await?;
        if source.metadata.get("channel_type") != Some(&Value::String("im".into())) {
            self.inner
                .subscriptions
                .add(source.conversation_id.as_ref())
                .await?;
        }
        Ok(())
    }

    /// Run Socket Mode until closed. Dropping this future requests bounded cleanup.
    pub async fn run<F, Fut>(&self, receiver: F) -> Result<()>
    where
        F: Fn(Message) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<()>> + Send + 'static,
    {
        self.run_socket(receiver).await
    }

    /// Stop intake immediately without waiting for the calling receiver to finish.
    pub fn request_close(&self) {
        self.stop_admission();
        loop {
            match self.inner.lifecycle.load(Ordering::Acquire) {
                NEW => {
                    if self
                        .inner
                        .lifecycle
                        .compare_exchange(NEW, CLOSED, Ordering::AcqRel, Ordering::Acquire)
                        .is_ok()
                    {
                        self.inner.stop.cancel();
                        self.inner.done.cancel();
                        return;
                    }
                }
                RUNNING => {
                    if self
                        .inner
                        .lifecycle
                        .compare_exchange(RUNNING, CLOSING, Ordering::AcqRel, Ordering::Acquire)
                        .is_ok()
                    {
                        self.inner.stop.cancel();
                        break;
                    }
                }
                CLOSING => break,
                _ => return,
            }
        }
    }

    /// Stop accepting new events while existing operations finish. Hosts may clear
    /// working indicators here before calling `close()` to release the transport.
    pub fn stop_admission(&self) {
        self.inner.accepting.store(false, Ordering::Release);
    }

    pub async fn close(&self) -> Result<()> {
        self.request_close();
        if self.in_delivery() {
            return Ok(());
        }
        self.inner.done.cancelled().await;
        Ok(())
    }

    async fn post_message(
        &self,
        source: &Message,
        destination: &Destination,
        mut payload: Value,
        content: &str,
    ) -> Result<Message> {
        payload["channel"] = Value::String(destination.channel_id.clone());
        if let Some(thread) = &destination.thread_timestamp {
            payload["thread_ts"] = Value::String(thread.to_string());
        }
        let response = self.inner.client.call("chat.postMessage", &payload).await?;
        let timestamp = events::nonempty(&response["ts"])
            .and_then(SlackTimestamp::parse)
            .ok_or_else(|| {
                Error::Transport("Slack reply delivery could not be confirmed".into())
            })?;
        Ok(Message {
            id: format!("slack:{}:{timestamp}", destination.channel_id).into(),
            conversation_id: source.conversation_id.clone(),
            channel: "slack".into(),
            sender: Sender {
                id: "agentchat".into(),
                display_name: Some("AgentChat".into()),
            },
            text: content.into(),
            role: Role::Assistant,
            thread_id: source.thread_id.clone(),
            addressed: true,
            attachments: Vec::new(),
            metadata: json!({"channel_id":destination.channel_id,
                "team_id":destination.team_id,"reply_thread_timestamp":destination.thread_timestamp,
                "message_timestamp":timestamp})
            .as_object()
            .cloned()
            .unwrap_or_default(),
        })
    }

    pub async fn reply(&self, source: &Message, content: &str) -> Result<Message> {
        let destination = self.destination(source).await?;
        let blocks = rendering::text_blocks(content)?;
        self.post_message(
            source,
            &destination,
            json!({"text":content,"blocks":blocks,
            "unfurl_links":false,"unfurl_media":false}),
            content,
        )
        .await
    }

    pub async fn reply_rich(&self, source: &Message, content: &RichReply) -> Result<Message> {
        let destination = self.destination(source).await?;
        self.post_message(
            source,
            &destination,
            rendering::rich_payload(content)?,
            &content.text,
        )
        .await
    }

    pub async fn set_status(&self, source: &Message, status: &str) -> Result<bool> {
        let destination = self.destination(source).await?;
        let Some(thread) = destination.thread_timestamp else {
            return Ok(false);
        };
        self.inner
            .client
            .call(
                "assistant.threads.setStatus",
                &json!({"channel_id":destination.channel_id,
            "thread_ts":thread,"status":status}),
            )
            .await?;
        Ok(true)
    }

    pub async fn mirror(
        &self,
        source: &Message,
        message: &Message,
        origin: &str,
    ) -> Result<Message> {
        let destination = self.destination(source).await?;
        let mut sent = self
            .post_message(
                source,
                &destination,
                rendering::mirror_payload(message, origin)?,
                &message.text,
            )
            .await?;
        sent.role = Role::User;
        sent.sender = message.sender.clone();
        sent.metadata.insert(
            "mirrored_message_id".into(),
            Value::String(message.id.to_string()),
        );
        sent.metadata
            .insert("origin".into(), Value::String(origin.into()));
        Ok(sent)
    }

    pub async fn get_user(&self, user_id: &str) -> Result<SlackUser> {
        self.identity().await?;
        let response = self
            .inner
            .client
            .get("users.info", &json!({"user":user_id}))
            .await?;
        let user = &response["user"];
        let invalid = || Error::Transport("Slack returned an invalid user profile".into());
        let profile = user["profile"].as_object().ok_or_else(invalid)?;
        if user["id"] != user_id {
            return Err(invalid());
        }
        let text = |value: &Value| {
            value
                .as_str()
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .map(str::to_owned)
        };
        Ok(SlackUser {
            id: user_id.into(),
            team_id: events::nonempty(&user["team_id"])
                .ok_or_else(invalid)?
                .into(),
            display_name: profile
                .get("display_name")
                .and_then(text)
                .or_else(|| profile.get("real_name").and_then(text))
                .or_else(|| text(&user["name"])),
            email: profile.get("email").and_then(text),
            is_bot: user["is_bot"].as_bool().ok_or_else(invalid)? || user["is_app_user"] == true,
            deleted: user["deleted"].as_bool().ok_or_else(invalid)?,
        })
    }

    pub async fn thread_history(&self, source: &Message, limit: usize) -> Result<Vec<Message>> {
        let limit = HistoryLimit::new(limit)?.get();
        let destination = self.destination(source).await?;
        let thread = destination
            .thread_timestamp
            .as_ref()
            .ok_or_else(|| Error::InvalidInput("Expected a Slack thread".into()))?;
        if thread.as_str() == destination.message_timestamp.as_str() {
            return Ok(Vec::new());
        }
        let identity = self.identity().await?;
        let mut root = None;
        let mut recent = VecDeque::new();
        let mut cursor = String::new();
        let mut seen = HashSet::new();
        for _ in 0..5 {
            let response = self
                .inner
                .client
                .get(
                    "conversations.replies",
                    &json!({
                "channel":destination.channel_id,"ts":thread,"latest":destination.message_timestamp,
                "inclusive":false,"limit":100,"cursor":cursor,"include_all_metadata":true}),
                )
                .await?;
            let records = response["messages"]
                .as_array()
                .ok_or_else(|| Error::Transport("Slack did not return thread history".into()))?;
            for record in records {
                let Some(ts) = events::nonempty(&record["ts"]).and_then(SlackTimestamp::parse)
                else {
                    continue;
                };
                let Some(sender_id) = events::nonempty(&record["user"])
                    .or_else(|| events::nonempty(&record["bot_id"]))
                else {
                    continue;
                };
                let text = record.get("text").map_or(Some(""), Value::as_str);
                let Some(text) = text else { continue };
                let files = events::attachments(&record["files"]);
                if (events::trim_python(text).is_empty() && files.is_empty())
                    || !ts.is_before(&destination.message_timestamp)
                {
                    continue;
                }
                let mirror = rendering::read_mirror(record, &identity.bot_user_id);
                let role = if sender_id == identity.bot_user_id && mirror.is_none() {
                    Role::Assistant
                } else {
                    Role::User
                };
                let input = mirror.unwrap_or_else(|| rendering::MirroredInput {
                    sender: Sender {
                        id: sender_id.into(),
                        display_name: None,
                    },
                    text: text.into(),
                });
                let message = Message {
                    id: format!(
                        "slack:{}:{}:{ts}",
                        identity.workspace_id, destination.channel_id
                    )
                    .into(),
                    conversation_id: source.conversation_id.clone(),
                    channel: "slack".into(),
                    sender: input.sender,
                    text: input.text,
                    role,
                    thread_id: Some(thread.to_string()),
                    addressed: true,
                    attachments: files,
                    metadata: json!({"message_timestamp":ts,"team_id":identity.workspace_id,
                        "channel_id":destination.channel_id,"reply_thread_timestamp":thread})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
                };
                if ts.as_str() == thread.as_str() {
                    root = Some(message);
                } else {
                    recent.push_back(message);
                    if recent.len() > limit - 1 {
                        recent.pop_front();
                    }
                }
            }
            if response["has_more"] != true {
                return Ok(root.into_iter().chain(recent).collect());
            }
            let next = events::nonempty(&response["response_metadata"]["next_cursor"]).ok_or_else(
                || Error::Transport("Slack returned invalid history pagination".into()),
            )?;
            if !seen.insert(next.to_owned()) {
                return Err(Error::Transport(
                    "Slack returned repeated history pagination".into(),
                ));
            }
            cursor = next.into();
        }
        Err(Error::InvalidInput(
            "Slack thread is too long; start a new thread".into(),
        ))
    }
}

#[async_trait]
impl Channel for Slack {
    fn name(&self) -> &str {
        "slack"
    }
    async fn reply(&self, source: &Message, content: &str) -> Result<Message> {
        Slack::reply(self, source, content).await
    }
    async fn close(&self) -> Result<()> {
        Slack::close(self).await
    }
    async fn thread_history(&self, source: &Message, limit: usize) -> Result<Option<Vec<Message>>> {
        Slack::thread_history(self, source, limit).await.map(Some)
    }
    async fn reply_rich(&self, source: &Message, content: &RichReply) -> Result<Message> {
        Slack::reply_rich(self, source, content).await
    }
    async fn set_status(&self, source: &Message, status: &str) -> Result<bool> {
        Slack::set_status(self, source, status).await
    }
    async fn mirror(&self, source: &Message, message: &Message, origin: &str) -> Result<Message> {
        Slack::mirror(self, source, message, origin).await
    }
    async fn upload_files(
        &self,
        source: &Message,
        files: &[UploadFile],
    ) -> Result<Vec<UploadedFile>> {
        Slack::upload_files(self, source, files).await
    }
    async fn download_attachment(
        &self,
        source: &Message,
        attachment: &Attachment,
        max_bytes: usize,
    ) -> Result<DownloadedFile> {
        Slack::download_attachment(self, source, attachment, max_bytes).await
    }
}
