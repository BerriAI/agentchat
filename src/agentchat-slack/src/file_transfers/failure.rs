//! Safe, content-free attachment read failures shared by transports and hosts.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FailureCode {
    AccessChanged,
    Authentication,
    CredentialUpstream,
    CredentialTransport,
    CredentialUnconfirmed,
    CredentialUnavailable,
    MissingScope,
    Unavailable,
    InvalidIdentifier,
    InvalidSize,
    HardLimit,
    UnsupportedLocation,
    Redirect,
    EncodedResponse,
    InvalidHeaders,
    PartialResponse,
    MetadataSizeMismatch,
    TruncatedStream,
    RateLimited,
    TransientHttp,
    Timeout,
    ReadBudget,
    Transport,
    Tls,
    Storage,
    InspectionTimeout,
    CanvasRepresentation,
    TransferSizeMismatch,
}
impl FailureCode {
    pub fn detail(self) -> (&'static str, &'static str) {
        match self {
            Self::AccessChanged => ("Slack access changed.", "Reconnect Slack and resend the attachment."),
            Self::Authentication => ("Could not read this Slack attachment because access was denied.", "Reconnect Slack and verify the bot can access the file."),
            Self::CredentialUpstream => ("Slack’s credential service returned an upstream error.", "Wait for the service to recover and check the saved Slack connection before retrying."),
            Self::CredentialTransport => ("The Slack credential service could not be reached.", "Check the network and saved Slack connection before retrying."),
            Self::CredentialUnconfirmed => ("The Slack credential acquisition result could not be confirmed.", "Check the saved Slack connection before retrying an unconfirmed token refresh."),
            Self::CredentialUnavailable => ("Slack bot credentials could not be obtained.", "Check the saved Slack connection state before retrying."),
            Self::MissingScope => ("Reconnect Slack with files:read permission.", "Reconnect Slack and resend the attachment."),
            Self::Unavailable => ("This Slack file is unavailable.", "Upload the attachment again or describe its contents."),
            Self::InvalidIdentifier => ("Expected a Slack file identifier.", "Resend the attachment from Slack."),
            Self::InvalidSize => ("Slack returned an invalid file size.", "Upload the attachment again."),
            Self::HardLimit => ("This file exceeds the workspace attachment storage budget.", "Ask an administrator to increase the budget, or send a smaller file."),
            Self::UnsupportedLocation => ("Slack returned an unsupported file location.", "Upload the attachment directly to Slack again."),
            Self::Redirect => ("Could not read this Slack attachment without following a redirect.", "Upload the attachment again; private-file redirects are not followed."),
            Self::EncodedResponse => ("Slack returned an encoded transfer despite the identity request.", "Retry later or upload the file again; compressed transfer bytes were not accepted as file bytes."),
            Self::InvalidHeaders => ("Slack returned unverifiable download headers.", "Retry later or upload the file again."),
            Self::PartialResponse => ("Slack returned only a partial file response.", "Retry later or upload the complete attachment again."),
            Self::MetadataSizeMismatch => ("Slack file did not match its declared size.", "Upload the attachment again or describe its contents; the complete file could not be verified."),
            Self::TruncatedStream => ("Slack file download ended with an incomplete stream.", "Retry later or upload the attachment again."),
            Self::RateLimited => ("Slack rate-limited the attachment read.", "Wait for Slack’s retry interval, then retry the attachment."),
            Self::TransientHttp => ("Slack temporarily could not serve the attachment.", "Retry later or describe the missing content."),
            Self::Timeout => ("Slack attachment read timed out.", "Retry later or send a smaller attachment."),
            Self::ReadBudget => ("Slack attachment recovery reached its time budget.", "Retry later or send a smaller attachment."),
            Self::Transport => ("The Slack attachment connection was interrupted.", "Retry later or upload the attachment again."),
            Self::Tls => ("The Slack attachment connection could not be verified.", "Ask an administrator to check the network connection."),
            Self::Storage => ("The attachment could not be written to temporary storage.", "Ask an administrator to check workspace storage."),
            Self::InspectionTimeout => ("The attachment could not be inspected within its time budget.", "Send a smaller attachment or describe its contents."),
            Self::CanvasRepresentation => ("Slack did not return a readable Canvas HTML export.", "Retry the Canvas export or provide its text; the response was not used as document content."),
            Self::TransferSizeMismatch => ("The downloaded attachment did not match its HTTP Content-Length.", "Retry later; incomplete or inconsistent transfer bytes were not retained."),
        }
    }
}
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReadStage {
    #[default]
    Access,
    Metadata,
    Headers,
    Download,
    RetryWait,
    Inspection,
}
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct Attempt {
    pub stage: ReadStage,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code: Option<FailureCode>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub declared_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub received_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content_length_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content_encoding: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content_encoding_present: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub http_status: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub representation: Option<String>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ReadFailure {
    pub code: FailureCode,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default)]
    pub retry_after: Option<f64>,
    #[serde(default)]
    pub history: Vec<Attempt>,
    #[serde(default)]
    pub retry_blocker: String,
}
impl ReadFailure {
    pub fn new(code: FailureCode) -> Self {
        Self {
            code,
            retryable: false,
            retry_after: None,
            history: Vec::new(),
            retry_blocker: String::new(),
        }
    }
    pub fn retry(code: FailureCode) -> Self {
        Self {
            retryable: true,
            ..Self::new(code)
        }
    }
    pub fn diagnostic(&self) -> Value {
        let latest = self.history.last().cloned().unwrap_or_default();
        let mut value = json!(latest);
        let attempts = latest.attempt.unwrap_or(0);
        let (message, action) = self.code.detail();
        let data = value.as_object_mut().expect("Attempt is an object");
        data.extend(json!({"phase":"slack_attachment_read","code":self.code,"attempts":attempts,
            "attempt_history":self.history,"recovery_tried": if attempts>1 {"A read-only retry was attempted; the attempt history identifies the stage reached."} else {"No retry was made."},
            "remaining_blocker":if self.retry_blocker.is_empty(){message}else{&self.retry_blocker},"next_action":action}).as_object().expect("object").clone());
        if let Some(seconds) = self.retry_after {
            data.insert("retry_after_seconds".into(), json!(seconds));
        }
        value
    }
}
impl std::fmt::Display for ReadFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let detail = self.diagnostic();
        let string = |key: &str| detail[key].as_str().unwrap_or_default().to_owned();
        write!(
            f,
            "{} [stage={}, code={}, attempts={}",
            self.code.detail().0,
            string("stage"),
            string("code"),
            detail["attempts"]
        )?;
        for key in ["declared_bytes", "received_bytes", "content_encoding"] {
            if let Some(value) = detail.get(key) {
                write!(
                    f,
                    ", {key}={}",
                    value
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| value.to_string())
                )?;
            }
        }
        write!(
            f,
            "] {} Remaining blocker: {} Next action: {}",
            string("recovery_tried"),
            string("remaining_blocker"),
            string("next_action")
        )
    }
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TransportFailure {
    pub code: String,
    #[serde(default)]
    pub status_code: Option<u16>,
    #[serde(default)]
    pub dispatched: Option<bool>,
    #[serde(default)]
    pub rejected: bool,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default)]
    pub retry_after: Option<f64>,
}
pub fn retry_seconds(value: Option<&str>) -> Option<f64> {
    value?
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite() && *v >= 0.0)
}
pub fn transient(status: Option<u16>) -> bool {
    matches!(status, Some(408 | 500 | 502 | 503 | 504))
}
impl TransportFailure {
    pub fn into_read_failure(self, stage: ReadStage) -> ReadFailure {
        use FailureCode::*;
        let auth = matches!(
            self.code.as_str(),
            "invalid_auth"
                | "token_revoked"
                | "not_authed"
                | "account_inactive"
                | "invalid_client_id"
                | "bad_client_secret"
                | "invalid_refresh_token"
                | "invalid_grant"
        );
        let rate = self.status_code == Some(429)
            || matches!(self.code.as_str(), "ratelimited" | "rate_limited");
        let mut failure = if matches!(self.status_code, Some(401 | 403)) || auth {
            ReadFailure::new(Authentication)
        } else if self.code == "missing_scope" {
            ReadFailure::new(MissingScope)
        } else if stage == ReadStage::Access {
            let code = if rate {
                RateLimited
            } else if transient(self.status_code) {
                CredentialUpstream
            } else if self.code == "connection_unavailable" && self.dispatched == Some(false) {
                CredentialTransport
            } else if self.code == "response_unavailable"
                || (self.dispatched != Some(false) && !self.rejected)
            {
                CredentialUnconfirmed
            } else {
                CredentialUnavailable
            };
            let mut failure = ReadFailure::new(code);
            failure.retry_blocker="Token acquisition may perform an OAuth refresh POST; attachment recovery did not replay it.".into();
            failure
        } else if rate {
            ReadFailure::retry(RateLimited)
        } else if transient(self.status_code) || self.retryable {
            ReadFailure::retry(Transport)
        } else {
            ReadFailure::new(Unavailable)
        };
        if rate {
            failure.retry_after = self.retry_after.filter(|v| v.is_finite() && *v >= 0.0);
        }
        failure
    }
}
