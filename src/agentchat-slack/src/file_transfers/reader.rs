//! Bounded private-file acquisition. Hosts supply authorization and transport effects.
use super::{canvas, failure::*, file_location, FilePurpose};
use async_trait::async_trait;
use serde_json::Value;
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Default)]
pub struct ReadProgress {
    pub current: Attempt,
    pub history: Vec<Attempt>,
}
pub type ReadState = Arc<Mutex<ReadProgress>>;
pub fn update(state: &ReadState, change: impl FnOnce(&mut Attempt)) {
    change(&mut state.lock().expect("read state poisoned").current);
}
pub fn snapshot(state: &ReadState) -> Attempt {
    state.lock().expect("read state poisoned").current.clone()
}
pub enum ReadError<E> {
    Failure(ReadFailure),
    Foreign(E),
}
impl<E> From<ReadFailure> for ReadError<E> {
    fn from(value: ReadFailure) -> Self {
        Self::Failure(value)
    }
}
#[derive(Clone, Debug)]
pub struct FileMetadata {
    pub name: String,
    pub url: String,
    pub media_type: String,
    pub size: Option<u64>,
    pub canvas: bool,
}
impl FileMetadata {
    /// Compatibility transport calls have a URL but no metadata to compare.
    pub fn location(url: &str) -> Self {
        Self {
            name: String::new(),
            url: url.into(),
            media_type: String::new(),
            size: None,
            canvas: false,
        }
    }

    pub fn parse(
        id: &str,
        response: &Value,
        max_bytes: usize,
        state: &ReadState,
    ) -> Result<Self, ReadFailure> {
        use FailureCode::*;
        if response["ok"] == false && response["error"] == "missing_scope" {
            return Err(ReadFailure::new(MissingScope));
        }
        let file = &response["file"];
        if response["ok"] != true
            || !file.is_object()
            || file["id"] != id
            || (!file["is_external"].is_null() && file["is_external"] != false)
        {
            return Err(ReadFailure::new(Unavailable));
        }
        let canvas = canvas::is_canvas(file);
        let size = file["size"].as_u64();
        if !canvas && size.is_none() {
            return Err(ReadFailure::new(InvalidSize));
        }
        update(state, |s| {
            s.declared_bytes = size;
            s.representation = canvas.then(|| "canvas_html".into());
        });
        if !canvas && size.is_some_and(|size| size > max_bytes as u64) {
            return Err(ReadFailure::new(HardLimit));
        }
        let url = file["url_private_download"]
            .as_str()
            .filter(|v| !v.is_empty())
            .or_else(|| file["url_private"].as_str())
            .ok_or_else(|| ReadFailure::new(UnsupportedLocation))?;
        let name = file["name"]
            .as_str()
            .filter(|v| !v.is_empty())
            .unwrap_or("attachment");
        let mut name = crate::events::filename(name)
            .chars()
            .take(180)
            .collect::<String>();
        if canvas && !name.to_lowercase().ends_with(".html") {
            name = format!("{}.html", name.chars().take(175).collect::<String>());
        }
        Ok(Self {
            name,
            url: url.into(),
            media_type: file["mimetype"].as_str().unwrap_or_default().into(),
            size,
            canvas,
        })
    }
}
#[derive(Default)]
pub struct DownloadHeaders {
    pub status: u16,
    pub encoding: Option<String>,
    pub length: Option<String>,
    pub content_range: Option<String>,
    pub content_type: Option<String>,
    pub retry_after: Option<String>,
}
pub struct TransferVerifier {
    max_bytes: u64,
    declared: Option<u64>,
    length: Option<u64>,
    canvas: bool,
    received: u64,
    state: ReadState,
}
impl TransferVerifier {
    pub fn new(
        metadata: &FileMetadata,
        headers: DownloadHeaders,
        max_bytes: usize,
        state: ReadState,
    ) -> Result<Self, ReadFailure> {
        use FailureCode::*;
        let encoding = headers
            .encoding
            .as_deref()
            .unwrap_or("identity")
            .trim()
            .to_ascii_lowercase();
        update(&state, |s| {
            s.http_status = Some(headers.status);
            s.content_encoding_present = Some(headers.encoding.is_some());
            s.content_encoding = Some(
                if matches!(
                    encoding.as_str(),
                    "identity" | "gzip" | "br" | "deflate" | "zstd"
                ) {
                    encoding.clone()
                } else {
                    "unsupported".into()
                },
            );
        });
        if headers.status == 429 {
            return Err(ReadFailure {
                retry_after: retry_seconds(headers.retry_after.as_deref()),
                ..ReadFailure::retry(RateLimited)
            });
        }
        if matches!(headers.status, 401 | 403) {
            return Err(ReadFailure::new(Authentication));
        }
        if (300..400).contains(&headers.status) {
            return Err(ReadFailure::new(Redirect));
        }
        if transient(Some(headers.status)) {
            return Err(ReadFailure::retry(TransientHttp));
        }
        if headers.status == 206
            || headers
                .content_range
                .as_ref()
                .is_some_and(|v| !v.is_empty())
        {
            return Err(ReadFailure::retry(PartialResponse));
        }
        if headers.status != 200 {
            return Err(ReadFailure::new(Unavailable));
        }
        if encoding != "identity" {
            return Err(ReadFailure::retry(EncodedResponse));
        }
        if metadata.canvas {
            let valid = headers
                .content_type
                .as_ref()
                .and_then(|v| v.parse::<mime::Mime>().ok())
                .is_some_and(|v| {
                    v.essence_str() == "text/html"
                        && v.get_param(mime::CHARSET)
                            .is_none_or(|v| matches!(v.as_str(), "utf-8" | "utf8"))
                });
            if !valid {
                return Err(ReadFailure::new(CanvasRepresentation));
            }
        }
        let length = if let Some(value) = headers.length {
            if value.is_empty() || value.len() > 20 || !value.bytes().all(|v| v.is_ascii_digit()) {
                return Err(ReadFailure::retry(InvalidHeaders));
            }
            let length = value
                .parse::<u64>()
                .map_err(|_| ReadFailure::retry(InvalidHeaders))?;
            update(&state, |s| s.content_length_bytes = Some(length));
            if length > max_bytes as u64 {
                return Err(ReadFailure::new(HardLimit));
            }
            if !metadata.canvas && metadata.size.is_some_and(|size| size != length) {
                return Err(ReadFailure::retry(MetadataSizeMismatch));
            }
            Some(length)
        } else {
            None
        };
        update(&state, |s| s.stage = ReadStage::Download);
        Ok(Self {
            max_bytes: max_bytes as u64,
            declared: metadata.size,
            length,
            canvas: metadata.canvas,
            received: 0,
            state,
        })
    }
    pub fn accept(&mut self, count: usize) -> Result<(), ReadFailure> {
        self.received = self.received.saturating_add(count as u64);
        update(&self.state, |s| s.received_bytes = Some(self.received));
        if self.received > self.max_bytes {
            return Err(ReadFailure::new(FailureCode::HardLimit));
        }
        if self.length.is_some_and(|n| self.received > n) {
            return Err(ReadFailure::retry(FailureCode::TransferSizeMismatch));
        }
        if !self.canvas && self.declared.is_some_and(|n| self.received > n) {
            return Err(ReadFailure::retry(FailureCode::MetadataSizeMismatch));
        }
        Ok(())
    }
    pub fn finish(&self) -> Result<(), ReadFailure> {
        update(&self.state, |s| s.received_bytes = Some(self.received));
        if self.length.is_some_and(|n| n != self.received) {
            return Err(ReadFailure::retry(FailureCode::TruncatedStream));
        }
        verify_size(self.declared, self.canvas, self.received)?;
        Ok(())
    }
}
pub fn verify_size(declared: Option<u64>, canvas: bool, received: u64) -> Result<(), ReadFailure> {
    if !canvas && declared.is_some_and(|size| size != received) {
        return Err(ReadFailure::retry(FailureCode::MetadataSizeMismatch));
    }
    if canvas && received == 0 {
        return Err(ReadFailure::new(FailureCode::CanvasRepresentation));
    }
    Ok(())
}
#[async_trait]
pub trait ReadTransport: Sync {
    type Error: Send;
    type File: Send;
    async fn authorize(&self) -> Result<(), ReadError<Self::Error>>;
    async fn token(&self, state: &ReadState) -> Result<String, ReadError<Self::Error>>;
    async fn metadata(
        &self,
        id: &str,
        token: &str,
        state: &ReadState,
    ) -> Result<Value, ReadError<Self::Error>>;
    async fn download(
        &self,
        file: &FileMetadata,
        token: &str,
        max_bytes: usize,
        state: &ReadState,
    ) -> Result<Self::File, ReadError<Self::Error>>;
    async fn sleep(&self, seconds: f64) -> Result<(), ReadError<Self::Error>>;
}
pub struct VerifiedFile<F> {
    pub metadata: FileMetadata,
    pub file: F,
    pub history: Vec<Attempt>,
    pub state: Attempt,
}
pub struct FileReader {
    pub max_bytes: usize,
    pub attempts: usize,
    pub budget: Duration,
    pub retry_delay: f64,
    cooldown: Mutex<Option<Instant>>,
}
impl FileReader {
    pub fn new(
        max_bytes: usize,
        attempts: usize,
        budget: f64,
        retry_delay: f64,
    ) -> Result<Self, ReadFailure> {
        if max_bytes == 0
            || attempts == 0
            || !budget.is_finite()
            || budget <= 0.0
            || budget > 86_400.0
            || !retry_delay.is_finite()
            || retry_delay < 0.0
        {
            return Err(ReadFailure::new(FailureCode::InvalidSize));
        }
        Ok(Self {
            max_bytes,
            attempts,
            budget: Duration::from_secs_f64(budget),
            retry_delay,
            cooldown: Mutex::new(None),
        })
    }
    fn rate_limit(&self) -> Result<(), ReadFailure> {
        if let Some(until) = *self.cooldown.lock().expect("cooldown poisoned") {
            if let Some(remaining) = until.checked_duration_since(Instant::now()) {
                return Err(ReadFailure {
                    retry_after: Some(remaining.as_secs_f64()),
                    ..ReadFailure::retry(FailureCode::RateLimited)
                });
            }
        }
        Ok(())
    }
    async fn attempt<T: ReadTransport>(
        &self,
        id: &str,
        transport: &T,
        state: &ReadState,
    ) -> Result<(FileMetadata, T::File), ReadError<T::Error>> {
        transport.authorize().await?;
        if !crate::events::valid_file_id(id) {
            return Err(ReadFailure::new(FailureCode::InvalidIdentifier).into());
        }
        update(state, |s| s.stage = ReadStage::Metadata);
        self.rate_limit()?;
        update(state, |s| s.stage = ReadStage::Access);
        let token = transport.token(state).await?;
        transport.authorize().await?;
        update(state, |s| s.stage = ReadStage::Metadata);
        let response = transport.metadata(id, &token, state).await?;
        transport.authorize().await?;
        let file = FileMetadata::parse(id, &response, self.max_bytes, state)?;
        if !file.canvas && file.size == Some(0) {
            return Err(ReadFailure::new(FailureCode::InvalidSize).into());
        }
        file_location(&file.url, FilePurpose::Download, None)
            .map_err(|_| ReadFailure::new(FailureCode::UnsupportedLocation))?;
        update(state, |s| s.stage = ReadStage::Headers);
        self.rate_limit()?;
        let raw = transport
            .download(&file, &token, self.max_bytes, state)
            .await?;
        transport.authorize().await?;
        Ok((file, raw))
    }
    pub async fn read<T: ReadTransport>(
        &self,
        id: &str,
        transport: &T,
        state: &ReadState,
    ) -> Result<VerifiedFile<T::File>, ReadError<T::Error>> {
        let deadline = Instant::now() + self.budget;
        let mut history = Vec::new();
        for attempt in 1..=self.attempts {
            update(state, |s| {
                *s = Attempt {
                    attempt: Some(attempt),
                    ..Attempt::default()
                }
            });
            match self.attempt(id, transport, state).await {
                Ok((metadata, file)) => {
                    return Ok(VerifiedFile {
                        metadata,
                        file,
                        history,
                        state: snapshot(state),
                    })
                }
                Err(ReadError::Foreign(error)) => return Err(ReadError::Foreign(error)),
                Err(ReadError::Failure(mut error)) => {
                    if error.code == FailureCode::RateLimited {
                        if let Some(delay) = error.retry_after {
                            let until = Duration::try_from_secs_f64(delay)
                                .ok()
                                .and_then(|duration| Instant::now().checked_add(duration));
                            let mut cooldown = self.cooldown.lock().expect("cooldown poisoned");
                            *cooldown = (*cooldown).max(until);
                        }
                    }
                    let mut current = snapshot(state);
                    current.code = Some(error.code);
                    history.push(current);
                    error.history = history.clone();
                    state.lock().expect("read state poisoned").history = history.clone();
                    if !error.retryable || attempt == self.attempts {
                        return Err(error.into());
                    }
                    // Independent readers spread ordinary retries. Provider intervals are never shortened.
                    let jitter = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .subsec_nanos() as f64
                        / 1_000_000_000.0
                        + 0.5;
                    let delay = if error.code == FailureCode::RateLimited {
                        error.retry_after
                    } else {
                        Some(self.retry_delay * jitter)
                    };
                    let Some(delay) = delay else {
                        error.retry_blocker="Slack did not supply a usable Retry-After interval; no early retry was made.".into();
                        return Err(error.into());
                    };
                    if delay
                        >= deadline
                            .saturating_duration_since(Instant::now())
                            .as_secs_f64()
                    {
                        error.retry_blocker = if error.code == FailureCode::RateLimited {
                            "Slack’s retry interval exceeds the remaining read budget; no early retry was made."
                        } else {
                            "The remaining read budget is too short for another attempt."
                        }.into();
                        return Err(error.into());
                    }
                    update(state, |s| s.stage = ReadStage::RetryWait);
                    transport.sleep(delay).await?;
                }
            }
        }
        unreachable!("validated nonzero attempts")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn headers_and_chunks_have_independent_bounds() {
        let state = Arc::new(Mutex::new(ReadProgress::default()));
        let file=FileMetadata::parse("F1",&json!({"ok":true,"file":{"id":"F1","size":5,"url_private":"https://files.slack.com/files-pri/file"}}),10,&state).unwrap();
        let mut verifier = TransferVerifier::new(
            &file,
            DownloadHeaders {
                status: 200,
                length: Some("5".into()),
                ..Default::default()
            },
            10,
            state.clone(),
        )
        .unwrap();
        verifier.accept(3).unwrap();
        assert_eq!(
            verifier.finish().unwrap_err().code,
            FailureCode::TruncatedStream
        );
        assert_eq!(
            verifier.accept(3).unwrap_err().code,
            FailureCode::TransferSizeMismatch
        );
        assert_eq!(snapshot(&state).received_bytes, Some(6));
    }

    #[test]
    fn unsafe_encodings_are_reported_without_copying_provider_text() {
        let state = Arc::new(Mutex::new(ReadProgress::default()));
        let file = FileMetadata::location("https://files.slack.com/files-pri/file");
        let result = TransferVerifier::new(
            &file,
            DownloadHeaders {
                status: 200,
                encoding: Some("provider secret".into()),
                ..Default::default()
            },
            10,
            state.clone(),
        );
        assert!(matches!(
            result,
            Err(ReadFailure {
                code: FailureCode::EncodedResponse,
                ..
            })
        ));
        assert_eq!(
            snapshot(&state).content_encoding.as_deref(),
            Some("unsupported")
        );
    }
}

#[cfg(test)]
mod compatibility_tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    #[test]
    fn url_only_transport_still_verifies_content_length_without_declared_size() {
        let state = Arc::new(Mutex::new(ReadProgress::default()));
        let file = FileMetadata::location("https://files.slack.com/files-pri/file");
        let mut verifier = TransferVerifier::new(
            &file,
            DownloadHeaders {
                status: 200,
                length: Some("5".into()),
                ..Default::default()
            },
            10,
            state,
        )
        .unwrap();
        verifier.accept(5).unwrap();
        verifier.finish().unwrap();
    }

    struct RetainedFile(Arc<AtomicBool>);
    impl Drop for RetainedFile {
        fn drop(&mut self) {
            self.0.store(true, Ordering::SeqCst);
        }
    }
    struct Transport {
        checks: AtomicUsize,
        closed: Arc<AtomicBool>,
    }
    #[async_trait]
    impl ReadTransport for Transport {
        type Error = &'static str;
        type File = RetainedFile;
        async fn authorize(&self) -> Result<(), ReadError<Self::Error>> {
            if self.checks.fetch_add(1, Ordering::SeqCst) == 3 {
                Err(ReadError::Foreign("revoked"))
            } else {
                Ok(())
            }
        }
        async fn token(&self, _: &ReadState) -> Result<String, ReadError<Self::Error>> {
            Ok("token".into())
        }
        async fn metadata(
            &self,
            _: &str,
            _: &str,
            _: &ReadState,
        ) -> Result<Value, ReadError<Self::Error>> {
            Ok(
                json!({"ok":true,"file":{"id":"F1","size":5,"url_private":"https://files.slack.com/files-pri/file"}}),
            )
        }
        async fn download(
            &self,
            _: &FileMetadata,
            _: &str,
            _: usize,
            _: &ReadState,
        ) -> Result<Self::File, ReadError<Self::Error>> {
            Ok(RetainedFile(self.closed.clone()))
        }
        async fn sleep(&self, _: f64) -> Result<(), ReadError<Self::Error>> {
            unreachable!()
        }
    }
    #[tokio::test]
    async fn core_rechecks_authority_after_transfer_before_publishing() {
        let transport = Transport {
            checks: AtomicUsize::new(0),
            closed: Arc::new(AtomicBool::new(false)),
        };
        let state = Arc::new(Mutex::new(ReadProgress::default()));
        let result = FileReader::new(100, 2, 90.0, 0.0)
            .unwrap()
            .read("F1", &transport, &state)
            .await;
        assert!(matches!(result, Err(ReadError::Foreign("revoked"))));
        assert!(transport.closed.load(Ordering::SeqCst));
        assert_eq!(transport.checks.load(Ordering::SeqCst), 4);
    }
}
