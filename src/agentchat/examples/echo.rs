//! Run with `cargo run -p agentchat --example echo`.
use std::sync::Arc;

use agentchat::{AgentChat, Channel, Message, Result, Role, Sender};
use async_trait::async_trait;

struct Console;

#[async_trait]
impl Channel for Console {
    fn name(&self) -> &str {
        "console"
    }
    async fn reply(&self, source: &Message, content: &str) -> Result<Message> {
        println!("{content}");
        Ok(Message {
            id: format!("{}:reply", source.id).into(),
            sender: Sender {
                id: "echo".into(),
                display_name: None,
            },
            text: content.into(),
            role: Role::Assistant,
            ..source.clone()
        })
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let app = Arc::new(AgentChat::new());
    let message: Message = serde_json::from_value(serde_json::json!({
        "id": "console:1", "conversation_id": "console:demo", "channel": "console",
        "sender": {"id": "person"}, "text": "Hello from Rust", "role": "user"
    }))
    .expect("valid example message");
    app.receive(Arc::new(Console), message, |context| async move {
        Ok(Some(format!("Echo: {}", context.message.text)))
    })
    .await
}
