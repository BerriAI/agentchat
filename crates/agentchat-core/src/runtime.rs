use std::{future::Future, sync::Arc};

use async_trait::async_trait;

use crate::{
    Attachment, Channel, ConversationId, ConversationPermit, DownloadedFile, Error, MemoryState,
    Message, MessageContext, Result, RichReply, Role, State, Turn, TurnEffect, TurnResult,
    UploadFile, UploadedFile,
};

/// An application handler; callbacks remain application-owned, never model-owned.
#[async_trait]
pub trait Handler: Send + Sync {
    async fn handle(&self, context: MessageContext) -> Result<Option<String>>;
}

#[async_trait]
impl<F, Fut> Handler for F
where
    F: Fn(MessageContext) -> Fut + Send + Sync,
    Fut: Future<Output = Result<Option<String>>> + Send,
{
    async fn handle(&self, context: MessageContext) -> Result<Option<String>> {
        self(context).await
    }
}

/// The runtime deliberately does not own the Tokio runtime or agent framework.
pub struct AgentChat {
    state: Arc<dyn State>,
}

pub type Runtime = AgentChat;

impl Default for AgentChat {
    fn default() -> Self {
        Self::new()
    }
}

impl AgentChat {
    pub fn new() -> Self {
        Self::with_state(Arc::new(MemoryState::new()))
    }

    pub fn with_state(state: Arc<dyn State>) -> Self {
        Self { state }
    }

    pub fn state(&self) -> &Arc<dyn State> {
        &self.state
    }

    pub async fn receive<F, Fut>(
        self: &Arc<Self>,
        channel: Arc<dyn Channel>,
        message: Message,
        handler: F,
    ) -> Result<()>
    where
        F: FnOnce(MessageContext) -> Fut,
        Fut: Future<Output = Result<Option<String>>>,
    {
        let mut turn = Turn::new();
        let mut handler = Some(handler);
        let mut confirmed_reply = None;
        // Keep the permit until every effect completes, including reply append.
        let mut _permit: Option<Box<dyn ConversationPermit>> = None;
        loop {
            let outcome = match turn.effect() {
                TurnEffect::Claim => TurnResult::Claimed(self.state.claim(&message.id).await?),
                TurnEffect::AcquireConversation => {
                    _permit = Some(self.state.acquire(&message.conversation_id).await?);
                    TurnResult::ConversationAcquired
                }
                TurnEffect::AppendIncoming => {
                    self.state.append(message.clone()).await?;
                    TurnResult::IncomingAppended
                }
                TurnEffect::InvokeHandler => {
                    let handler = handler.take().ok_or_else(|| {
                        Error::InvalidTransition("handler already invoked".into())
                    })?;
                    let context =
                        MessageContext::new(self.clone(), channel.clone(), message.clone());
                    TurnResult::HandlerCompleted(handler(context).await?)
                }
                TurnEffect::SendReply(content) => {
                    confirmed_reply = Some(channel.reply(&message, content).await?);
                    TurnResult::ReplySent
                }
                TurnEffect::AppendReply => {
                    let reply = confirmed_reply.take().ok_or_else(|| {
                        Error::InvalidTransition("reply was not confirmed".into())
                    })?;
                    self.state.append(reply).await?;
                    TurnResult::ReplyAppended
                }
                TurnEffect::Complete => return Ok(()),
            };
            turn.resolve(outcome)?;
        }
    }

    pub async fn receive_with_handler(
        self: &Arc<Self>,
        channel: Arc<dyn Channel>,
        message: Message,
        handler: &dyn Handler,
    ) -> Result<()> {
        self.receive(channel, message, |context| handler.handle(context))
            .await
    }

    pub async fn reply(
        &self,
        channel: Arc<dyn Channel>,
        source: &Message,
        content: &str,
    ) -> Result<Message> {
        let reply = channel.reply(source, content).await?;
        self.state.append(reply.clone()).await?;
        Ok(reply)
    }

    pub async fn reply_rich(
        &self,
        channel: Arc<dyn Channel>,
        source: &Message,
        content: &RichReply,
    ) -> Result<Message> {
        let reply = channel.reply_rich(source, content).await?;
        self.state.append(reply.clone()).await?;
        Ok(reply)
    }

    pub async fn upload_files(
        &self,
        channel: Arc<dyn Channel>,
        source: &Message,
        files: &[UploadFile],
    ) -> Result<Vec<UploadedFile>> {
        channel.upload_files(source, files).await
    }

    pub async fn download_attachment(
        &self,
        channel: Arc<dyn Channel>,
        source: &Message,
        attachment: &Attachment,
        max_bytes: usize,
    ) -> Result<DownloadedFile> {
        if !source.attachments.contains(attachment) {
            return Err(Error::InvalidInput(
                "Attachment does not belong to the source message".into(),
            ));
        }
        channel
            .download_attachment(source, attachment, max_bytes)
            .await
    }

    pub async fn set_status(
        &self,
        channel: Arc<dyn Channel>,
        source: &Message,
        status: &str,
    ) -> Result<bool> {
        channel.set_status(source, status).await
    }

    pub async fn mirror(
        &self,
        channel: Arc<dyn Channel>,
        source: &Message,
        message: &Message,
        origin: &str,
    ) -> Result<Message> {
        if message.role != Role::User {
            return Err(Error::InvalidInput(
                "Only user inputs can be mirrored".into(),
            ));
        }
        let mirrored = channel.mirror(source, message, origin).await?;
        self.state.append(mirrored.clone()).await?;
        Ok(mirrored)
    }

    pub async fn history(
        &self,
        conversation: &ConversationId,
        limit: Option<usize>,
    ) -> Result<Vec<Message>> {
        self.state.history(conversation, limit).await
    }
}
