use std::{sync::Arc, time::Duration};

use tokio::{sync::Mutex, task::JoinHandle};

use crate::{Channel, Error, Message, Result};

const STATUS_TIMEOUT: Duration = Duration::from_secs(3);

/// Refreshes a transient indicator without putting it in message history.
///
/// Call [`Self::close`] to await clearing the indicator before shutting down its
/// transport. Rust cannot await in `Drop`: dropping aborts refresh and schedules
/// best-effort clearing only when a Tokio runtime remains available. Transport
/// failures never replace an application error or prevent an answer.
#[must_use = "call close().await to ensure the working indicator is cleared"]
pub struct WorkingStatus {
    channel: Arc<dyn Channel>,
    source: Message,
    status: Arc<Mutex<String>>,
    worker: Option<JoinHandle<()>>,
    closed: bool,
}

async fn send(channel: &dyn Channel, source: &Message, status: &str) -> bool {
    matches!(
        tokio::time::timeout(STATUS_TIMEOUT, channel.set_status(source, status)).await,
        Ok(Ok(true))
    )
}

impl WorkingStatus {
    pub async fn start(
        channel: Arc<dyn Channel>,
        source: Message,
        status: String,
        refresh_interval: Duration,
    ) -> Result<Self> {
        if refresh_interval.is_zero() || refresh_interval > Duration::from_secs(90) {
            return Err(Error::InvalidInput(
                "Refresh interval must be greater than 0 and at most 90 seconds".into(),
            ));
        }
        let status = Arc::new(Mutex::new(status));
        send(channel.as_ref(), &source, &status.lock().await).await;
        let worker_channel = channel.clone();
        let worker_source = source.clone();
        let worker_status = status.clone();
        let worker = tokio::spawn(async move {
            loop {
                tokio::time::sleep(refresh_interval).await;
                let current = worker_status.lock().await;
                send(worker_channel.as_ref(), &worker_source, &current).await;
            }
        });
        Ok(Self {
            channel,
            source,
            status,
            worker: Some(worker),
            closed: false,
        })
    }

    /// Update the label used for future refreshes. Unsupported/outage returns false.
    pub async fn update(&self, status: &str) -> bool {
        if self.closed {
            return false;
        }
        let mut current = self.status.lock().await;
        *current = status.to_owned();
        send(self.channel.as_ref(), &self.source, &current).await
    }

    pub async fn close(&mut self) {
        if self.closed {
            return;
        }
        if let Some(worker) = self.worker.take() {
            worker.abort();
            let _ = worker.await;
        }
        self.update("").await;
        self.closed = true;
    }
}

impl Drop for WorkingStatus {
    fn drop(&mut self) {
        if self.closed {
            return;
        }
        let worker = self.worker.take();
        if let Some(worker) = &worker {
            worker.abort();
        }
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            let channel = self.channel.clone();
            let source = self.source.clone();
            runtime.spawn(async move {
                if let Some(worker) = worker {
                    let _ = worker.await;
                }
                send(channel.as_ref(), &source, "").await;
            });
        }
    }
}
