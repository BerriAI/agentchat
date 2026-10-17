//! Native HTTP effects for the shared, bounded file-transfer engine.
use super::{
    file_transfers::{self, DownloadBuffer, FileTransport, TransferError},
    Slack,
};
use agentchat_core::{
    Attachment, DownloadedFile, Error, Message, Result, Role, UploadFile, UploadedFile,
};
use async_trait::async_trait;
use futures_util::StreamExt;
use serde_json::Value;

fn transfer_error(error: TransferError<Error>) -> Error {
    match error {
        TransferError::Policy(error) | TransferError::Transport(error) => error,
    }
}

#[async_trait]
impl FileTransport for Slack {
    type Error = Error;
    async fn request(&self, method: &str, payload: &Value) -> Result<Value> {
        match method {
            "files.getUploadURLExternal" => self
                .inner
                .client
                .form(method, payload)
                .await
                .map_err(Into::into),
            "files.info" => self
                .inner
                .client
                .get(method, payload)
                .await
                .map_err(Into::into),
            _ => self
                .inner
                .client
                .call(method, payload)
                .await
                .map_err(Into::into),
        }
    }
    async fn upload(&self, url: &str, bytes: &[u8]) -> Result<()> {
        let url = self
            .inner
            .client
            .file_url(url, file_transfers::FilePurpose::Upload)?;
        let response = self
            .inner
            .client
            .http
            .post(url)
            .header("Content-Type", "application/octet-stream")
            .body(bytes.to_vec())
            .send()
            .await
            .map_err(|_| {
                Error::Transport("Slack file transfer failed; delivery may be uncertain".into())
            })?;
        if response.status() != reqwest::StatusCode::OK {
            return Err(Error::Transport(
                "Slack did not confirm the file upload".into(),
            ));
        }
        Ok(())
    }
    async fn download(&self, url: &str, token: &str, max_bytes: usize) -> Result<Vec<u8>> {
        self.download_file(
            &file_transfers::reader::FileMetadata::location(url),
            token,
            max_bytes,
        )
        .await
    }
    async fn download_file(
        &self,
        file: &file_transfers::reader::FileMetadata,
        token: &str,
        max_bytes: usize,
    ) -> Result<Vec<u8>> {
        let url = self
            .inner
            .client
            .file_url(&file.url, file_transfers::FilePurpose::Download)?;
        let response = self
            .inner
            .client
            .http
            .get(url)
            .bearer_auth(token)
            .header("Accept-Encoding", "identity")
            .send()
            .await
            .map_err(|_| Error::Transport("Slack file download failed".into()))?;
        let header = |name: &str| {
            response
                .headers()
                .get(name)
                .map(|v| v.to_str().unwrap_or("invalid").to_owned())
        };
        let state = std::sync::Arc::new(std::sync::Mutex::new(
            file_transfers::reader::ReadProgress::default(),
        ));
        let policy_error =
            |error: file_transfers::failure::ReadFailure| Error::InvalidInput(error.to_string());
        let mut verifier = file_transfers::reader::TransferVerifier::new(
            file,
            file_transfers::reader::DownloadHeaders {
                status: response.status().as_u16(),
                encoding: header("Content-Encoding"),
                length: header("Content-Length"),
                content_range: header("Content-Range"),
                content_type: header("Content-Type"),
                retry_after: header("Retry-After"),
            },
            max_bytes,
            state,
        )
        .map_err(policy_error)?;
        let mut stream = response.bytes_stream();
        let mut buffer = DownloadBuffer::new(max_bytes);
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| Error::Transport("Slack file download failed".into()))?;
            verifier.accept(chunk.len()).map_err(policy_error)?;
            buffer.append(&chunk)?;
        }
        verifier.finish().map_err(policy_error)?;
        buffer.finish()
    }
    async fn guard(&self) -> Result<()> {
        self.check_open()
    }
}
impl Slack {
    pub async fn upload_files(
        &self,
        source: &Message,
        files: &[UploadFile],
    ) -> Result<Vec<UploadedFile>> {
        let destination = self.destination(source).await?;
        file_transfers::upload(
            files,
            &destination.channel_id,
            destination.thread_timestamp.as_ref().map(|ts| ts.as_str()),
            self,
        )
        .await
        .map_err(transfer_error)
    }
    pub async fn download_attachment(
        &self,
        source: &Message,
        attachment: &Attachment,
        max_bytes: usize,
    ) -> Result<DownloadedFile> {
        self.destination(source).await?;
        if source.role != Role::User || !source.attachments.contains(attachment) {
            return Err(Error::InvalidInput(
                "Attachment does not belong to the source user message".into(),
            ));
        }
        file_transfers::download(attachment, self.inner.client.bot_token(), max_bytes, self)
            .await
            .map_err(transfer_error)
    }
}
