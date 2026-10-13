//! Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN, then run:
//! `cargo run -p agentchat --features slack --example slack_echo`.
use std::sync::Arc;

use agentchat::{slack::Slack, AgentChat, Error, Result};

#[tokio::main]
async fn main() -> Result<()> {
    let app = Arc::new(AgentChat::new());
    let slack = Arc::new(Slack::from_env()?);
    let receiver_slack = slack.clone();
    let receive = move |message| {
        let app = app.clone();
        let slack = receiver_slack.clone();
        async move {
            // This demo echoes every admitted message. Production applications
            // authorize the sender here before subscribing or invoking an agent.
            slack.subscribe(&message).await?;
            app.receive(slack, message, |context| async move {
                Ok(Some(context.message.text.clone()))
            })
            .await
        }
    };
    let outcome = tokio::select! {
        result = slack.run(receive) => result,
        signal = tokio::signal::ctrl_c() => signal.map_err(|error| Error::Transport(error.to_string())),
    };
    slack.close().await?;
    outcome
}
