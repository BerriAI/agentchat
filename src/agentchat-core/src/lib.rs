//! Shared policy and an asynchronous runtime for application-owned agents.
//!
//! The turn state machine is also driven by foreign-language hosts. It owns the
//! ordering policy while effects and user callbacks execute on their host loop.

mod activity;
mod channel;
mod context;
mod error;
mod history;
mod models;
mod runtime;
mod state;
mod turn;

pub use activity::WorkingStatus;
pub use channel::Channel;
pub use context::{Conversation, MessageContext};
pub use error::{Error, Result};
pub use history::{select_history, thread_history_indices, HistoryLimit};
pub use models::*;
pub use runtime::{AgentChat, Handler, Runtime};
pub use state::{ConversationPermit, MemoryState, MemoryStore, State};
pub use turn::{Turn, TurnEffect, TurnResult};
