//! Web API transport for applications that own HTTP ingress and durable delivery.
//! OAuth and refresh remain application-owned; no Socket Mode token is needed.
use crate::transport::WebClient;
use agentchat_core::Result;
use serde_json::Value;

#[derive(Clone)]
pub struct SlackWebApi {
    client: WebClient,
}

impl SlackWebApi {
    pub fn new(bot_token: String) -> Result<Self> {
        Self::with_api_base(bot_token, "https://slack.com/api/")
    }

    /// Use only a trusted application-configured proxy or loopback test server.
    pub fn with_api_base(bot_token: String, api_base: &str) -> Result<Self> {
        if bot_token.trim().is_empty() {
            return Err(agentchat_core::Error::InvalidInput(
                "Slack bot_token is required".into(),
            ));
        }
        Ok(Self {
            client: WebClient::new(api_base, bot_token, String::new())?,
        })
    }

    /// One attempt. The application owns retries after uncertain writes.
    pub async fn post(&self, method: &str, payload: &Value) -> Result<Value> {
        validate_method(method)?;
        self.client.call(method, payload).await.map_err(Into::into)
    }

    pub async fn get(&self, method: &str, query: &Value) -> Result<Value> {
        validate_method(method)?;
        self.client.get(method, query).await.map_err(Into::into)
    }
}

fn validate_method(method: &str) -> Result<()> {
    if method.is_empty()
        || !method
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'.')
        || method.starts_with('.')
        || method.ends_with('.')
        || method.contains("..")
    {
        return Err(agentchat_core::Error::InvalidInput(
            "Expected a Slack Web API method".into(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{extract::Query, http::HeaderMap, routing::get, Json, Router};

    #[tokio::test]
    async fn host_ingress_uses_only_a_bot_token_and_preserves_provider_receipts() {
        async fn read(headers: HeaderMap, Query(query): Query<Value>) -> Json<Value> {
            assert_eq!(headers["authorization"], "Bearer bot-secret");
            assert_eq!(query["channel"], "C123");
            Json(serde_json::json!({"ok":true,"messages":[]}))
        }
        async fn post(headers: HeaderMap, Json(body): Json<Value>) -> Json<Value> {
            assert_eq!(headers["authorization"], "Bearer bot-secret");
            assert_eq!(body["ts"], "123.456");
            Json(serde_json::json!({"ok":true,"channel":"C123","ts":"123.456"}))
        }
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let app = Router::new()
            .route("/conversations.history", get(read))
            .route("/chat.update", axum::routing::post(post));
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let api = SlackWebApi::with_api_base("bot-secret".into(), &base).unwrap();
        assert_eq!(
            api.get(
                "conversations.history",
                &serde_json::json!({"channel":"C123"})
            )
            .await
            .unwrap()["ok"],
            true
        );
        assert_eq!(
            api.post(
                "chat.update",
                &serde_json::json!({"channel":"C123","ts":"123.456"})
            )
            .await
            .unwrap()["ts"],
            "123.456"
        );
        server.abort();
        assert!(SlackWebApi::new(String::new()).is_err());
        assert!(SlackWebApi::with_api_base("bot".into(), "http://example.com").is_err());
        for method in [
            "https://example.com",
            "/chat.update",
            "../chat.update",
            "chat.update?token=x",
        ] {
            assert!(api.post(method, &serde_json::json!({})).await.is_err());
        }
    }
}
