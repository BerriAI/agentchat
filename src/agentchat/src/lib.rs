//! AgentChat's native Rust interface. Enable a provider feature to use its transport.
pub use agentchat_core::*;

#[cfg(feature = "slack")]
pub use agentchat_slack as slack;

/// Portable provider policy; channel transports are exposed separately.
#[cfg(feature = "providers")]
pub use agentchat_providers as providers;
