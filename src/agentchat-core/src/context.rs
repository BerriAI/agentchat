use std::sync::Arc;

use crate::{
    AgentChat, Attachment, Channel, ConversationId, DownloadedFile, HistoryLimit, Message, Result,
    RichReply, Sender, UploadFile, UploadedFile,
};

#[derive(Clone)]
pub struct Conversation {
    pub id: ConversationId,
    pub(crate) app: Arc<AgentChat>,
}

impl Conversation {
    pub async fn history(&self, limit: Option<usize>) -> Result<Vec<Message>> {
        self.app.history(&self.id, limit).await
    }
}

#[derive(Clone)]
pub struct MessageContext {
    pub message: Message,
    pub sender: Sender,
    pub conversation: Conversation,
    pub(crate) app: Arc<AgentChat>,
    pub(crate) channel: Arc<dyn Channel>,
}

impl MessageContext {
    /// Begin an indicator; await its `close()` before closing the channel.
    pub async fn working(
        &self,
        status: &str,
        refresh_interval: std::time::Duration,
    ) -> Result<crate::WorkingStatus> {
        crate::WorkingStatus::start(
            self.channel.clone(),
            self.message.clone(),
            status.to_owned(),
            refresh_interval,
        )
        .await
    }

    pub fn new(app: Arc<AgentChat>, channel: Arc<dyn Channel>, message: Message) -> Self {
        Self {
            sender: message.sender.clone(),
            conversation: Conversation {
                id: message.conversation_id.clone(),
                app: app.clone(),
            },
            message,
            app,
            channel,
        }
    }

    pub async fn history(&self, limit: usize) -> Result<Vec<Message>> {
        let limit = HistoryLimit::new(limit)?;
        if self.message.thread_id.is_some() {
            if let Some(mut previous) = self
                .channel
                .thread_history(&self.message, limit.get())
                .await?
            {
                previous.push(self.message.clone());
                return Ok(limit.select(&previous));
            }
        }
        self.conversation.history(Some(limit.get())).await
    }

    pub async fn reply(&self, content: &str) -> Result<Message> {
        self.app
            .reply(self.channel.clone(), &self.message, content)
            .await
    }

    pub async fn reply_rich(&self, content: &RichReply) -> Result<Message> {
        self.app
            .reply_rich(self.channel.clone(), &self.message, content)
            .await
    }

    pub async fn upload_files(&self, files: &[UploadFile]) -> Result<Vec<UploadedFile>> {
        self.app
            .upload_files(self.channel.clone(), &self.message, files)
            .await
    }

    pub async fn download_attachment(
        &self,
        attachment: &Attachment,
        max_bytes: usize,
    ) -> Result<DownloadedFile> {
        self.app
            .download_attachment(self.channel.clone(), &self.message, attachment, max_bytes)
            .await
    }

    pub async fn set_status(&self, status: &str) -> Result<bool> {
        self.app
            .set_status(self.channel.clone(), &self.message, status)
            .await
    }
}
