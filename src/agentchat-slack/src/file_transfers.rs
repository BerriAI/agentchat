//! File-transfer policy independent of the HTTP or foreign-SDK transport.
pub mod canvas;
pub mod failure;
pub mod reader;
use agentchat_core::{Attachment, DownloadedFile, Error, UploadFile, UploadedFile};
use async_trait::async_trait;
use reqwest::Url;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

pub enum TransferError<E> {
    Policy(Error),
    Transport(E),
}
impl<E> From<Error> for TransferError<E> {
    fn from(error: Error) -> Self {
        Self::Policy(error)
    }
}

#[async_trait]
pub trait FileTransport: Sync {
    type Error: Send;
    async fn request(&self, method: &str, payload: &Value) -> Result<Value, Self::Error>;
    async fn upload(&self, url: &str, bytes: &[u8]) -> Result<(), Self::Error>;
    async fn download(
        &self,
        url: &str,
        token: &str,
        max_bytes: usize,
    ) -> Result<Vec<u8>, Self::Error>;
    /// Metadata-aware transports override this to verify headers before buffering.
    /// The default preserves existing implementations; the caller still verifies bytes.
    async fn download_file(
        &self,
        file: &reader::FileMetadata,
        token: &str,
        max_bytes: usize,
    ) -> Result<Vec<u8>, Self::Error> {
        self.download(&file.url, token, max_bytes).await
    }
    async fn guard(&self) -> Result<(), Self::Error>;
}

#[derive(Clone, Copy, Debug)]
pub enum FilePurpose {
    Upload,
    Download,
}
impl FilePurpose {
    fn path_prefix(self) -> &'static str {
        match self {
            Self::Upload => "/upload/",
            Self::Download => "/files-pri/",
        }
    }
    fn error(self) -> Error {
        Error::InvalidInput(
            match self {
                Self::Upload => "Slack returned an unsupported upload destination",
                Self::Download => "Slack returned an unsupported file location",
            }
            .into(),
        )
    }
}

/// Validate once at the owning policy layer, then pass a parsed URL to HTTP.
/// Only a native client explicitly configured for a loopback API may use that
/// same loopback origin for local fixture transfers.
pub fn file_location(
    value: &str,
    purpose: FilePurpose,
    loopback_base: Option<&Url>,
) -> agentchat_core::Result<Url> {
    let url = Url::parse(value).map_err(|_| purpose.error())?;
    let is_loopback =
        |url: &Url| matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    let trusted_loopback = loopback_base.is_some_and(|base| {
        matches!(base.scheme(), "http" | "https")
            && is_loopback(base)
            && is_loopback(&url)
            && url.host_str() == base.host_str()
            && url.port_or_known_default() == base.port_or_known_default()
            && url.scheme() == base.scheme()
    });
    let trusted_slack = url.scheme() == "https"
        && url.host_str() == Some("files.slack.com")
        && matches!(url.port(), None | Some(443));
    if !(trusted_loopback || trusted_slack)
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || !url.path().starts_with(purpose.path_prefix())
    {
        return Err(purpose.error());
    }
    Ok(url)
}

pub fn validate_file_url(value: &str, upload: bool) -> agentchat_core::Result<()> {
    file_location(
        value,
        if upload {
            FilePurpose::Upload
        } else {
            FilePurpose::Download
        },
        None,
    )
    .map(|_| ())
}

pub fn validate_upload(file: &UploadFile) -> agentchat_core::Result<()> {
    if file.filename.is_empty()
        || file.filename.len() > 255
        || matches!(file.filename.as_str(), "." | "..")
        || file.filename.contains(['/', '\\'])
        || file.filename.chars().any(|c| (c as u32) < 32)
    {
        return Err(Error::InvalidInput("Expected a plain filename".into()));
    }
    if file.content.is_empty() {
        return Err(Error::InvalidInput("Expected nonempty file bytes".into()));
    }
    Ok(())
}

pub fn download_limit_error(max_bytes: usize) -> Error {
    Error::InvalidInput(format!(
        "Slack file exceeds the {} MB download limit.",
        max_bytes as f64 / (1024.0 * 1024.0)
    ))
}

pub struct DownloadBuffer {
    bytes: Vec<u8>,
    limit: usize,
}
impl DownloadBuffer {
    pub fn new(limit: usize) -> Self {
        Self {
            bytes: Vec::new(),
            limit,
        }
    }
    pub fn append(&mut self, chunk: &[u8]) -> agentchat_core::Result<()> {
        if chunk.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(download_limit_error(self.limit));
        }
        self.bytes.extend_from_slice(chunk);
        Ok(())
    }
    pub fn finish(self) -> agentchat_core::Result<Vec<u8>> {
        if self.bytes.is_empty() {
            Err(Error::InvalidInput(
                "This Slack file was empty. Upload the attachment again.".into(),
            ))
        } else {
            Ok(self.bytes)
        }
    }
}

pub async fn upload<T: FileTransport>(
    files: &[UploadFile],
    channel_id: &str,
    thread_ts: Option<&str>,
    transport: &T,
) -> Result<Vec<UploadedFile>, TransferError<T::Error>> {
    super::policy::ReplyDestination::validate(channel_id, thread_ts)?;
    if files.is_empty() {
        return Err(Error::InvalidInput("Expected at least one file".into()).into());
    }
    for file in files {
        validate_upload(file)?;
    }
    let mut pending = Vec::new();
    let mut ids = HashSet::new();
    for file in files {
        transport.guard().await.map_err(TransferError::Transport)?;
        let allocation = transport
            .request(
                "files.getUploadURLExternal",
                &json!({"filename":file.filename,"length":file.content.len()}),
            )
            .await
            .map_err(TransferError::Transport)?;
        let id = super::events::nonempty(&allocation["file_id"]);
        let url = super::events::nonempty(&allocation["upload_url"]);
        if allocation["ok"] != true
            || id.is_none()
            || url.is_none()
            || !ids.insert(id.unwrap_or_default().to_owned())
        {
            return Err(
                Error::Transport("Slack did not confirm an upload allocation".into()).into(),
            );
        }
        transport.guard().await.map_err(TransferError::Transport)?;
        transport
            .upload(url.unwrap_or_default(), &file.content)
            .await
            .map_err(TransferError::Transport)?;
        pending.push(json!({"id":id,"title":file.title.as_deref().filter(|s|!s.is_empty()).unwrap_or(&file.filename)}));
    }
    transport.guard().await.map_err(TransferError::Transport)?;
    let mut payload = json!({"files":pending,"channel_id":channel_id});
    if let Some(thread) = thread_ts {
        payload["thread_ts"] = thread.into();
    }
    let result = transport
        .request("files.completeUploadExternal", &payload)
        .await
        .map_err(TransferError::Transport)?;
    let invalid = || Error::Transport("Slack media delivery could not be confirmed".into());
    let completed = result["files"].as_array().ok_or_else(invalid)?;
    let by_id: HashMap<_, _> = completed
        .iter()
        .filter_map(|item| item["id"].as_str().map(|id| (id, item)))
        .collect();
    if result["ok"] != true
        || completed.len() != pending.len()
        || by_id.len() != pending.len()
        || ids.iter().any(|id| !by_id.contains_key(id.as_str()))
    {
        return Err(invalid().into());
    }
    pending
        .iter()
        .map(|file| {
            let id = file["id"].as_str().ok_or_else(invalid)?;
            Ok(UploadedFile {
                id: id.into(),
                permalink: by_id[id]["permalink"].as_str().map(str::to_owned),
            })
        })
        .collect()
}

pub async fn download<T: FileTransport>(
    attachment: &Attachment,
    bot_token: &str,
    max_bytes: usize,
    transport: &T,
) -> Result<DownloadedFile, TransferError<T::Error>> {
    if !super::events::valid_file_id(&attachment.id) {
        return Err(Error::InvalidInput("Expected a Slack file identifier".into()).into());
    }
    if max_bytes == 0 {
        return Err(Error::InvalidInput("Expected a positive attachment byte limit".into()).into());
    }
    if bot_token.is_empty() {
        return Err(Error::InvalidInput(
            "A Slack bot token is required for attachment downloads".into(),
        )
        .into());
    }
    transport.guard().await.map_err(TransferError::Transport)?;
    let info = transport
        .request("files.info", &json!({"file":attachment.id}))
        .await
        .map_err(TransferError::Transport)?;
    let state = std::sync::Arc::new(std::sync::Mutex::new(reader::ReadProgress::default()));
    let file = reader::FileMetadata::parse(&attachment.id, &info, max_bytes, &state).map_err(
        |failure| match failure.code {
            failure::FailureCode::HardLimit => download_limit_error(max_bytes),
            _ => Error::InvalidInput(failure.code.detail().0.into()),
        },
    )?;
    transport.guard().await.map_err(TransferError::Transport)?;
    let bytes = transport
        .download_file(&file, bot_token, max_bytes)
        .await
        .map_err(TransferError::Transport)?;
    transport.guard().await.map_err(TransferError::Transport)?;
    let mut buffer = DownloadBuffer::new(max_bytes);
    buffer.append(&bytes)?;
    if !bytes.is_empty() {
        reader::verify_size(file.size, file.canvas, bytes.len() as u64)
            .map_err(|error| Error::InvalidInput(error.to_string()))?;
    }
    Ok(DownloadedFile {
        id: attachment.id.clone(),
        name: file.name,
        content: buffer.finish()?,
        media_type: (!file.media_type.is_empty()).then_some(file.media_type),
    })
}

#[cfg(test)]
mod location_tests {
    use super::*;

    #[test]
    fn file_locations_use_one_strict_policy() {
        assert!(file_location(
            "https://files.slack.com/upload/v1",
            FilePurpose::Upload,
            None
        )
        .is_ok());
        for value in [
            "https://files.slack.com.evil.test/upload/v1",
            "https://user@files.slack.com/upload/v1",
            "https://files.slack.com:444/upload/v1",
            "https://files.slack.com/files-pri/x",
        ] {
            assert!(file_location(value, FilePurpose::Upload, None).is_err());
        }
    }

    #[test]
    fn local_file_origins_require_the_configured_test_origin() {
        let base = Url::parse("http://127.0.0.1:1234/api/").unwrap();
        assert!(file_location(
            "http://127.0.0.1:1234/files-pri/a",
            FilePurpose::Download,
            Some(&base)
        )
        .is_ok());
        assert!(file_location(
            "http://127.0.0.1:1234/files-pri/a",
            FilePurpose::Download,
            None
        )
        .is_err());
        assert!(file_location(
            "http://127.0.0.1:1235/files-pri/a",
            FilePurpose::Download,
            Some(&base)
        )
        .is_err());
    }
}
