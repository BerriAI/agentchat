use async_trait::async_trait;

use crate::{
    Attachment, DownloadedFile, Error, Message, Result, RichReply, UploadFile, UploadedFile,
};

/// A transport returns a message only after provider confirmation.
/// Failed or ambiguous sends must not be transparently retried.
#[async_trait]
pub trait Channel: Send + Sync {
    fn name(&self) -> &str;

    async fn reply(&self, source: &Message, content: &str) -> Result<Message>;

    async fn close(&self) -> Result<()> {
        Ok(())
    }

    /// `None` means unsupported; errors from supported history must propagate.
    async fn thread_history(
        &self,
        _source: &Message,
        _limit: usize,
    ) -> Result<Option<Vec<Message>>> {
        Ok(None)
    }

    async fn reply_rich(&self, _source: &Message, _content: &RichReply) -> Result<Message> {
        Err(Error::Unsupported("rich replies"))
    }

    async fn upload_files(
        &self,
        _source: &Message,
        _files: &[UploadFile],
    ) -> Result<Vec<UploadedFile>> {
        Err(Error::Unsupported("file uploads"))
    }

    async fn download_attachment(
        &self,
        _source: &Message,
        _attachment: &Attachment,
        _max_bytes: usize,
    ) -> Result<DownloadedFile> {
        Err(Error::Unsupported("attachment downloads"))
    }

    async fn set_status(&self, _source: &Message, _status: &str) -> Result<bool> {
        Ok(false)
    }

    async fn mirror(
        &self,
        _source: &Message,
        _message: &Message,
        _origin: &str,
    ) -> Result<Message> {
        Err(Error::Unsupported("mirroring"))
    }
}
