//! Explicit, bounded file transfer. Events never cause an automatic download.

use std::collections::{HashMap, HashSet};

use agentchat_core::{
    Attachment, DownloadedFile, Error, Message, Result, Role, UploadFile, UploadedFile,
};
use futures_util::StreamExt;
use serde_json::{json, Value};

use super::{events, Slack};

impl Slack {
    pub async fn upload_files(
        &self,
        source: &Message,
        files: &[UploadFile],
    ) -> Result<Vec<UploadedFile>> {
        let destination = self.destination(source).await?;
        if files.is_empty() {
            return Err(Error::InvalidInput("Expected at least one file".into()));
        }
        for file in files {
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
        }
        let mut pending = Vec::new();
        let mut seen = HashSet::new();
        for file in files {
            self.check_open()?;
            let allocation = self
                .inner
                .client
                .form(
                    "files.getUploadURLExternal",
                    &json!({
                "filename":file.filename,"length":file.content.len()}),
                )
                .await?;
            let id = events::nonempty(&allocation["file_id"]).ok_or_else(|| {
                Error::Transport("Slack did not confirm an upload allocation".into())
            })?;
            if !seen.insert(id.to_owned()) {
                return Err(Error::Transport(
                    "Slack returned a duplicate upload allocation".into(),
                ));
            }
            let url = allocation["upload_url"]
                .as_str()
                .ok_or_else(|| Error::Transport("Slack did not provide an upload URL".into()))?;
            let url = self.inner.client.file_url(url, "/upload/")?;
            self.check_open()?;
            let uploaded = self
                .inner
                .client
                .http
                .post(url)
                .header("Content-Type", "application/octet-stream")
                .body(file.content.clone())
                .send()
                .await
                .map_err(|_| {
                    Error::Transport("Slack file transfer failed; delivery may be uncertain".into())
                })?;
            if uploaded.status() != reqwest::StatusCode::OK {
                return Err(Error::Transport(
                    "Slack did not confirm the file upload".into(),
                ));
            }
            pending.push(json!({"id":id,"title":file.title.as_deref().filter(|title| !title.is_empty()).unwrap_or(&file.filename)}));
        }
        self.check_open()?;
        let mut payload = json!({"files":pending,"channel_id":destination.channel_id});
        if let Some(thread) = destination.thread_timestamp {
            payload["thread_ts"] = Value::String(thread.to_string());
        }
        let response = self
            .inner
            .client
            .call("files.completeUploadExternal", &payload)
            .await?;
        let completed = response["files"]
            .as_array()
            .ok_or_else(|| Error::Transport("Slack file delivery could not be confirmed".into()))?;
        let by_id: HashMap<_, _> = completed
            .iter()
            .filter_map(|file| file["id"].as_str().map(|id| (id, file)))
            .collect();
        if completed.len() != pending.len()
            || by_id.len() != pending.len()
            || seen.iter().any(|id| !by_id.contains_key(id.as_str()))
        {
            return Err(Error::Transport(
                "Slack file delivery could not be confirmed".into(),
            ));
        }
        pending
            .iter()
            .map(|file| {
                let id = file["id"]
                    .as_str()
                    .ok_or_else(|| Error::Transport("Invalid upload allocation".into()))?;
                Ok(UploadedFile {
                    id: id.into(),
                    permalink: by_id[id]["permalink"].as_str().map(str::to_owned),
                })
            })
            .collect()
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
        if !events::valid_file_id(&attachment.id) {
            return Err(Error::InvalidInput(
                "Expected a Slack file identifier".into(),
            ));
        }
        if max_bytes == 0 {
            return Err(Error::InvalidInput(
                "Expected a positive attachment byte limit".into(),
            ));
        }
        let response = self
            .inner
            .client
            .get("files.info", &json!({"file":attachment.id}))
            .await?;
        let file = &response["file"];
        if file["id"] != attachment.id || file["is_external"] == true {
            return Err(Error::InvalidInput("This Slack file is unavailable".into()));
        }
        let size = file["size"]
            .as_u64()
            .ok_or_else(|| Error::Transport("Slack returned an invalid file size".into()))?;
        let too_large = || {
            Error::InvalidInput(format!(
                "Slack file exceeds the {max_bytes} byte download limit"
            ))
        };
        if size > max_bytes as u64 {
            return Err(too_large());
        }
        let url = events::nonempty(&file["url_private_download"])
            .or_else(|| events::nonempty(&file["url_private"]))
            .ok_or_else(|| {
                Error::Transport("Slack did not return a private download URL".into())
            })?;
        let url = self.inner.client.file_url(url, "/files-pri/")?;
        self.check_open()?;
        let response = self
            .inner
            .client
            .http
            .get(url)
            .bearer_auth(self.inner.client.bot_token())
            .send()
            .await
            .map_err(|_| Error::Transport("Slack file download failed".into()))?;
        if response.status() != reqwest::StatusCode::OK {
            return Err(Error::Transport(
                "Could not read this Slack attachment".into(),
            ));
        }
        let mut chunks = response.bytes_stream();
        let mut content = Vec::new();
        while let Some(chunk) = chunks.next().await {
            let chunk = chunk.map_err(|_| Error::Transport("Slack file download failed".into()))?;
            if chunk.len() > max_bytes.saturating_sub(content.len()) {
                return Err(too_large());
            }
            content.extend_from_slice(&chunk);
        }
        self.check_open()?;
        if content.is_empty() {
            return Err(Error::InvalidInput("This Slack file was empty".into()));
        }
        Ok(DownloadedFile {
            id: attachment.id.clone(),
            name: file["name"]
                .as_str()
                .map(events::filename)
                .unwrap_or_else(|| "attachment".into()),
            content,
            media_type: file["mimetype"].as_str().map(str::to_owned),
        })
    }
}
