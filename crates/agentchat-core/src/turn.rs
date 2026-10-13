use crate::{Error, Result};

/// The next host effect. A turn never performs I/O itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TurnEffect {
    Claim,
    AcquireConversation,
    AppendIncoming,
    InvokeHandler,
    SendReply(String),
    AppendReply,
    Complete,
}

/// Successful effect outcomes. A failed effect stops the driver without retry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TurnResult {
    Claimed(bool),
    ConversationAcquired,
    IncomingAppended,
    HandlerCompleted(Option<String>),
    ReplySent,
    ReplyAppended,
}

#[derive(Debug, Clone)]
pub struct Turn {
    effect: TurnEffect,
}

impl Default for Turn {
    fn default() -> Self {
        Self::new()
    }
}

impl Turn {
    pub fn new() -> Self {
        Self {
            effect: TurnEffect::Claim,
        }
    }

    pub fn effect(&self) -> &TurnEffect {
        &self.effect
    }

    pub fn resolve(&mut self, result: TurnResult) -> Result<()> {
        self.effect = match (&self.effect, result) {
            (TurnEffect::Claim, TurnResult::Claimed(true)) => TurnEffect::AcquireConversation,
            (TurnEffect::Claim, TurnResult::Claimed(false)) => TurnEffect::Complete,
            (TurnEffect::AcquireConversation, TurnResult::ConversationAcquired) => {
                TurnEffect::AppendIncoming
            }
            (TurnEffect::AppendIncoming, TurnResult::IncomingAppended) => TurnEffect::InvokeHandler,
            (TurnEffect::InvokeHandler, TurnResult::HandlerCompleted(Some(text))) => {
                TurnEffect::SendReply(text)
            }
            (TurnEffect::InvokeHandler, TurnResult::HandlerCompleted(None)) => TurnEffect::Complete,
            (TurnEffect::SendReply(_), TurnResult::ReplySent) => TurnEffect::AppendReply,
            (TurnEffect::AppendReply, TurnResult::ReplyAppended) => TurnEffect::Complete,
            (effect, result) => {
                return Err(Error::InvalidTransition(format!(
                    "{effect:?} cannot accept {result:?}"
                )))
            }
        };
        Ok(())
    }
}
