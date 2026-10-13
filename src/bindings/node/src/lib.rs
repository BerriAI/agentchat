use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Weak};
use std::time::Duration;

use agentchat_core::{
    Attachment, Channel, ConversationId, Error as CoreError, Message, RichReply, Runtime,
    UploadFile,
};
use agentchat_slack::{Slack, SlackConfig};
use napi::bindgen_prelude::*;
use napi::threadsafe_function::ThreadsafeFunction;
use napi_derive::napi;
use serde::Deserialize;
use tokio::sync::Mutex;
use tokio::task::JoinSet;

type Handler = ThreadsafeFunction<
    FnArgs<(u32, String)>,
    Promise<Option<String>>,
    FnArgs<(u32, String)>,
    Status,
    false,
>;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct SlackOptions {
    bot_token: String,
    app_token: String,
    workspace_id: Option<String>,
    bot_user_id: Option<String>,
    #[serde(default = "default_ack_emoji")]
    ack_emoji: Option<String>,
    max_pending_events: Option<usize>,
    shutdown_timeout_ms: Option<u64>,
    api_base_url: Option<String>,
}

fn default_ack_emoji() -> Option<String> {
    Some("eyes".into())
}

#[napi(object)]
pub struct NativeUploadFile {
    pub filename: String,
    pub content: Buffer,
    pub title: Option<String>,
}

#[napi(object)]
pub struct NativeDownloadedFile {
    pub id: String,
    pub name: String,
    pub content: Buffer,
    pub media_type: Option<String>,
}

#[napi]
pub struct NativeWorkingStatus {
    inner: Arc<Mutex<agentchat_core::WorkingStatus>>,
}

#[napi]
impl NativeWorkingStatus {
    #[napi]
    pub async fn update(&self, status: String) -> bool {
        self.inner.lock().await.update(&status).await
    }

    #[napi]
    pub async fn close(&self) {
        self.inner.lock().await.close().await;
    }
}

fn native_error(error: impl std::fmt::Display) -> napi::Error {
    napi::Error::from_reason(error.to_string())
}

fn decode_message(value: &str) -> napi::Result<Message> {
    serde_json::from_str(value).map_err(native_error)
}

fn encode_message(value: &Message) -> napi::Result<String> {
    serde_json::to_string(value).map_err(native_error)
}

#[napi]
pub struct NativeAgentChat {
    runtime: Arc<Runtime>,
    channels: Vec<Arc<Slack>>,
    running: Arc<AtomicBool>,
    closed: Arc<AtomicBool>,
    closing: AtomicBool,
    close_gate: Mutex<()>,
    working: Mutex<Vec<Weak<Mutex<agentchat_core::WorkingStatus>>>>,
}

#[napi]
impl NativeAgentChat {
    #[napi(constructor)]
    pub fn new(channels_json: String) -> napi::Result<Self> {
        let options: Vec<SlackOptions> =
            serde_json::from_str(&channels_json).map_err(native_error)?;
        if options.is_empty() {
            return Err(native_error("AgentChat requires at least one channel"));
        }
        let channels = options
            .into_iter()
            .map(|options| {
                let mut config = SlackConfig::new(options.bot_token, options.app_token);
                config.workspace_id = options.workspace_id;
                config.bot_user_id = options.bot_user_id;
                config.ack_emoji = options.ack_emoji;
                if let Some(limit) = options.max_pending_events {
                    config.max_pending_events = limit;
                }
                if let Some(timeout) = options.shutdown_timeout_ms {
                    config.shutdown_timeout = Duration::from_millis(timeout);
                }
                if let Some(url) = options.api_base_url {
                    config.api_base_url = url;
                }
                Slack::new(config).map(Arc::new).map_err(native_error)
            })
            .collect::<napi::Result<Vec<_>>>()?;
        Ok(Self {
            runtime: Arc::new(Runtime::new()),
            channels,
            running: Arc::new(AtomicBool::new(false)),
            closed: Arc::new(AtomicBool::new(false)),
            closing: AtomicBool::new(false),
            close_gate: Mutex::new(()),
            working: Mutex::new(Vec::new()),
        })
    }

    #[napi(
        ts_args_type = "handler: (channelIndex: number, messageJson: string) => Promise<string | null>"
    )]
    pub async fn run(&self, handler: Arc<Handler>) -> napi::Result<()> {
        if self.closing.load(Ordering::Acquire) || self.closed.load(Ordering::Acquire) {
            return Err(native_error("AgentChat is closed"));
        }
        if self.running.swap(true, Ordering::AcqRel) {
            return Err(native_error("AgentChat is already running"));
        }
        let mut tasks = JoinSet::new();
        for (index, slack) in self.channels.iter().enumerate() {
            let slack = Arc::clone(slack);
            let runtime = Arc::clone(&self.runtime);
            let handler = Arc::clone(&handler);
            tasks.spawn(async move {
                let channel: Arc<dyn Channel> = slack.clone();
                slack
                    .run(move |message| {
                        let runtime = Arc::clone(&runtime);
                        let channel = Arc::clone(&channel);
                        let handler = Arc::clone(&handler);
                        async move {
                            runtime
                                .receive(channel, message, move |context| async move {
                                    let json = serde_json::to_string(&context.message)
                                        .map_err(|error| CoreError::Handler(error.to_string()))?;
                                    let promise = handler
                                        .call_async_catch((index as u32, json).into())
                                        .await
                                        .map_err(|error| CoreError::Handler(error.to_string()))?;
                                    promise
                                        .await
                                        .map_err(|error| CoreError::Handler(error.to_string()))
                                })
                                .await
                        }
                    })
                    .await
            });
        }
        let mut first_error = None;
        while let Some(result) = tasks.join_next().await {
            match result {
                Ok(Ok(())) => {}
                Ok(Err(error)) => {
                    first_error = Some(native_error(error));
                    break;
                }
                Err(error) => {
                    first_error = Some(native_error(error));
                    break;
                }
            }
        }
        if first_error.is_some() {
            let _ = self.close().await;
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
        }
        self.running.store(false, Ordering::Release);
        first_error.map_or(Ok(()), Err)
    }

    #[napi]
    pub async fn close(&self) -> napi::Result<()> {
        self.closing.store(true, Ordering::Release);
        for channel in &self.channels {
            channel.stop_admission();
        }
        let _close_guard = self.close_gate.lock().await;
        if self.closed.load(Ordering::Acquire) {
            return Ok(());
        }
        let mut first_error = None;
        let working: Vec<_> = self
            .working
            .lock()
            .await
            .drain(..)
            .filter_map(|status| status.upgrade())
            .collect();
        let mut status_closures = JoinSet::new();
        for status in working {
            status_closures.spawn(async move { status.lock().await.close().await });
        }
        while let Some(result) = status_closures.join_next().await {
            if let Err(error) = result {
                first_error.get_or_insert_with(|| native_error(error));
            }
        }
        let mut closures = JoinSet::new();
        for channel in &self.channels {
            let channel = Arc::clone(channel);
            closures.spawn(async move { channel.close().await });
        }
        while let Some(result) = closures.join_next().await {
            match result {
                Ok(Ok(())) => {}
                Ok(Err(error)) => {
                    first_error.get_or_insert_with(|| native_error(error));
                }
                Err(error) => {
                    first_error.get_or_insert_with(|| native_error(error));
                }
            }
        }
        self.closed.store(true, Ordering::Release);
        first_error.map_or(Ok(()), Err)
    }

    #[napi]
    pub async fn reply(
        &self,
        channel_index: u32,
        source_json: String,
        text: String,
    ) -> napi::Result<String> {
        if self.closed.load(Ordering::Acquire) {
            return Err(native_error("AgentChat is closed"));
        }
        let source = decode_message(&source_json)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let reply = slack
            .with_admitted_source(&source, self.runtime.reply(channel, &source, &text))
            .await
            .map_err(native_error)?;
        encode_message(&reply)
    }

    #[napi]
    pub async fn history(
        &self,
        conversation_id: String,
        limit: Option<u32>,
    ) -> napi::Result<String> {
        let history = self
            .runtime
            .history(
                &ConversationId(conversation_id),
                limit.map(|value| value as usize),
            )
            .await
            .map_err(native_error)?;
        serde_json::to_string(&history).map_err(native_error)
    }

    #[napi]
    pub async fn thread_history(
        &self,
        channel_index: u32,
        source_json: String,
        limit: u32,
    ) -> napi::Result<String> {
        let source = decode_message(&source_json)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let context =
            agentchat_core::MessageContext::new(Arc::clone(&self.runtime), channel, source.clone());
        let history = slack
            .with_admitted_source(&source, context.history(limit as usize))
            .await
            .map_err(native_error)?;
        serde_json::to_string(&history).map_err(native_error)
    }

    #[napi]
    pub async fn subscribe(&self, channel_index: u32, source_json: String) -> napi::Result<()> {
        let source = decode_message(&source_json)?;
        let slack = self.channel(channel_index)?;
        slack
            .with_admitted_source(&source, slack.subscribe(&source))
            .await
            .map_err(native_error)
    }

    #[napi]
    pub async fn reply_rich(
        &self,
        channel_index: u32,
        source_json: String,
        content_json: String,
    ) -> napi::Result<String> {
        let source = decode_message(&source_json)?;
        let content: RichReply = serde_json::from_str(&content_json).map_err(native_error)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let reply = slack
            .with_admitted_source(&source, self.runtime.reply_rich(channel, &source, &content))
            .await
            .map_err(native_error)?;
        encode_message(&reply)
    }

    #[napi]
    pub async fn upload_files(
        &self,
        channel_index: u32,
        source_json: String,
        files: Vec<NativeUploadFile>,
    ) -> napi::Result<String> {
        let source = decode_message(&source_json)?;
        let files: Vec<_> = files
            .into_iter()
            .map(|file| UploadFile {
                filename: file.filename,
                content: file.content.to_vec(),
                title: file.title,
            })
            .collect();
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let uploaded = slack
            .with_admitted_source(&source, self.runtime.upload_files(channel, &source, &files))
            .await
            .map_err(native_error)?;
        serde_json::to_string(&uploaded).map_err(native_error)
    }

    #[napi]
    pub async fn download_attachment(
        &self,
        channel_index: u32,
        source_json: String,
        attachment_json: String,
        max_bytes: u32,
    ) -> napi::Result<NativeDownloadedFile> {
        let source = decode_message(&source_json)?;
        let attachment: Attachment =
            serde_json::from_str(&attachment_json).map_err(native_error)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let file = slack
            .with_admitted_source(
                &source,
                self.runtime
                    .download_attachment(channel, &source, &attachment, max_bytes as usize),
            )
            .await
            .map_err(native_error)?;
        Ok(NativeDownloadedFile {
            id: file.id,
            name: file.name,
            content: file.content.into(),
            media_type: file.media_type,
        })
    }

    #[napi]
    pub async fn set_status(
        &self,
        channel_index: u32,
        source_json: String,
        status: String,
    ) -> napi::Result<bool> {
        let source = decode_message(&source_json)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        slack
            .with_admitted_source(&source, self.runtime.set_status(channel, &source, &status))
            .await
            .map_err(native_error)
    }

    #[napi]
    pub async fn working(
        &self,
        channel_index: u32,
        source_json: String,
        status: String,
        refresh_interval_ms: u32,
    ) -> napi::Result<NativeWorkingStatus> {
        // Serialize creation with the shutdown snapshot so a pending status cannot
        // appear after its channel transport has been closed.
        let mut working = self.working.lock().await;
        if self.closing.load(Ordering::Acquire) {
            return Err(native_error("AgentChat is closed"));
        }
        let source = decode_message(&source_json)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let context =
            agentchat_core::MessageContext::new(Arc::clone(&self.runtime), channel, source.clone());
        let inner = Arc::new(Mutex::new(
            slack
                .with_admitted_source(
                    &source,
                    context.working(&status, Duration::from_millis(refresh_interval_ms.into())),
                )
                .await
                .map_err(native_error)?,
        ));
        if self.closing.load(Ordering::Acquire) {
            inner.lock().await.close().await;
            return Err(native_error("AgentChat is closed"));
        }
        working.retain(|status| status.strong_count() > 0);
        working.push(Arc::downgrade(&inner));
        Ok(NativeWorkingStatus { inner })
    }

    #[napi]
    pub async fn mirror(
        &self,
        channel_index: u32,
        source_json: String,
        message_json: String,
        origin: String,
    ) -> napi::Result<String> {
        let source = decode_message(&source_json)?;
        let message = decode_message(&message_json)?;
        let slack = self.channel(channel_index)?;
        let channel: Arc<dyn Channel> = slack.clone();
        let mirrored = slack
            .with_admitted_source(
                &source,
                self.runtime.mirror(channel, &source, &message, &origin),
            )
            .await
            .map_err(native_error)?;
        encode_message(&mirrored)
    }
}

impl NativeAgentChat {
    fn channel(&self, index: u32) -> napi::Result<Arc<Slack>> {
        if self.closed.load(Ordering::Acquire) {
            return Err(native_error("AgentChat is closed"));
        }
        self.channels
            .get(index as usize)
            .cloned()
            .ok_or_else(|| native_error("Unknown channel"))
    }
}
