//! Single-attempt Web API transport. Errors deliberately omit credential-bearing URLs.

use std::time::Duration;

use agentchat_core::{Error, Result};
use reqwest::{Client, Url};
use serde_json::Value;

#[derive(Debug)]
pub(crate) enum RequestError {
    Http(u16),
    Api(String),
    Uncertain,
    InvalidResponse,
}

impl RequestError {
    pub(crate) fn retryable_read(&self) -> bool {
        matches!(self, Self::Http(429 | 500..=599) | Self::Uncertain)
            || matches!(self, Self::Api(code) if matches!(code.as_str(), "ratelimited" | "internal_error" | "service_unavailable"))
    }
}

impl From<RequestError> for Error {
    fn from(error: RequestError) -> Self {
        let message = match error {
            RequestError::Http(status) => format!("Slack API request failed (HTTP {status})"),
            RequestError::Api(code) => format!("Slack API rejected the request ({code})"),
            RequestError::Uncertain => "Slack request failed; delivery may be uncertain".into(),
            RequestError::InvalidResponse => "Slack did not return a valid API response".into(),
        };
        Error::Transport(message)
    }
}

#[derive(Clone)]
pub(crate) struct WebClient {
    pub(crate) http: Client,
    base_url: Url,
    bot_token: String,
    app_token: String,
}

pub(crate) fn loopback(url: &Url) -> bool {
    url.host_str()
        .is_some_and(|host| matches!(host, "localhost" | "127.0.0.1" | "[::1]" | "::1"))
}

impl WebClient {
    pub(crate) fn new(base_url: &str, bot_token: String, app_token: String) -> Result<Self> {
        let mut base_url = Url::parse(base_url)
            .map_err(|_| Error::InvalidInput("Invalid Slack API base URL".into()))?;
        if !(base_url.scheme() == "https" || (base_url.scheme() == "http" && loopback(&base_url)))
            || base_url.host_str().is_none()
            || !base_url.username().is_empty()
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
        {
            return Err(Error::InvalidInput("Slack API requires HTTPS (HTTP is allowed only on loopback) without credentials or query parameters".into()));
        }
        if !base_url.path().ends_with('/') {
            base_url.set_path(&format!("{}/", base_url.path()));
        }
        let http = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| Error::Transport("Could not create Slack HTTP client".into()))?;
        Ok(Self {
            http,
            base_url,
            bot_token,
            app_token,
        })
    }

    pub(crate) fn bot_token(&self) -> &str {
        &self.bot_token
    }

    pub(crate) fn file_url(&self, value: &str, path_prefix: &str) -> Result<Url> {
        let url = Url::parse(value)
            .map_err(|_| Error::InvalidInput("Slack returned an invalid file URL".into()))?;
        let trusted_loopback = loopback(&self.base_url)
            && loopback(&url)
            && url.host_str() == self.base_url.host_str()
            && url.port_or_known_default() == self.base_url.port_or_known_default()
            && url.scheme() == self.base_url.scheme();
        let trusted_slack = url.scheme() == "https"
            && url.host_str() == Some("files.slack.com")
            && matches!(url.port(), None | Some(443));
        if !(trusted_loopback || trusted_slack)
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
            || !url.path().starts_with(path_prefix)
        {
            return Err(Error::InvalidInput(
                "Slack returned an unsupported file destination".into(),
            ));
        }
        Ok(url)
    }

    pub(crate) async fn get(
        &self,
        method: &str,
        payload: &Value,
    ) -> std::result::Result<Value, RequestError> {
        let url = self
            .base_url
            .join(method)
            .map_err(|_| RequestError::InvalidResponse)?;
        let response = self
            .http
            .get(url)
            .bearer_auth(&self.bot_token)
            .query(payload)
            .send()
            .await
            .map_err(|_| RequestError::Uncertain)?;
        Self::response(response).await
    }

    pub(crate) fn validate_socket_url(&self, url: &str) -> Result<()> {
        let url = Url::parse(url)
            .map_err(|_| Error::Transport("Slack returned an invalid socket URL".into()))?;
        let valid = if loopback(&self.base_url) {
            matches!(url.scheme(), "ws" | "wss") && loopback(&url)
        } else {
            url.scheme() == "wss"
                && url
                    .host_str()
                    .is_some_and(|host| host.ends_with(".slack.com"))
        };
        if !valid
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(Error::Transport(
                "Slack returned an unsupported socket destination".into(),
            ));
        }
        Ok(())
    }

    pub(crate) async fn call(
        &self,
        method: &str,
        payload: &Value,
    ) -> std::result::Result<Value, RequestError> {
        self.request(method, payload, false).await
    }

    pub(crate) async fn form(
        &self,
        method: &str,
        payload: &Value,
    ) -> std::result::Result<Value, RequestError> {
        self.request(method, payload, true).await
    }

    async fn request(
        &self,
        method: &str,
        payload: &Value,
        form: bool,
    ) -> std::result::Result<Value, RequestError> {
        let url = self
            .base_url
            .join(method)
            .map_err(|_| RequestError::InvalidResponse)?;
        let token = if method == "apps.connections.open" {
            &self.app_token
        } else {
            &self.bot_token
        };
        let request = self.http.post(url).bearer_auth(token);
        let response = if form {
            request.form(payload)
        } else {
            request.json(payload)
        }
        .send()
        .await
        .map_err(|_| RequestError::Uncertain)?;
        Self::response(response).await
    }

    async fn response(response: reqwest::Response) -> std::result::Result<Value, RequestError> {
        if !response.status().is_success() {
            return Err(RequestError::Http(response.status().as_u16()));
        }
        let result: Value = response
            .json()
            .await
            .map_err(|_| RequestError::InvalidResponse)?;
        if result["ok"] != true {
            // Accept only Slack's machine-readable error code; never reflect arbitrary server text.
            let code = result["error"]
                .as_str()
                .filter(|code| {
                    code.len() <= 80 && code.bytes().all(|c| c.is_ascii_lowercase() || c == b'_')
                })
                .unwrap_or("unknown_error");
            return Err(RequestError::Api(code.into()));
        }
        Ok(result)
    }
}
