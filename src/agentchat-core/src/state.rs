use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use async_trait::async_trait;
use tokio::sync::{Mutex, OwnedMutexGuard};

use crate::{ConversationId, Message, MessageId, Result};

/// Value-independent memory storage. Claims and history deliberately have no TTL,
/// matching the original in-process state. Synchronization belongs to the caller.
#[derive(Debug)]
pub struct MemoryStore<T: Clone> {
    claims: HashSet<MessageId>,
    messages: HashMap<ConversationId, Vec<T>>,
}

impl<T: Clone> Default for MemoryStore<T> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T: Clone> MemoryStore<T> {
    pub fn new() -> Self {
        Self {
            claims: HashSet::new(),
            messages: HashMap::new(),
        }
    }

    pub fn claim(&mut self, id: &MessageId) -> bool {
        self.claims.insert(id.clone())
    }

    pub fn append(&mut self, conversation: &ConversationId, value: T) {
        self.messages
            .entry(conversation.clone())
            .or_default()
            .push(value);
    }

    /// Visit host-owned values so bindings can participate in garbage collection.
    pub fn values(&self) -> impl Iterator<Item = &T> {
        self.messages.values().flat_map(|messages| messages.iter())
    }

    pub fn clear(&mut self) {
        self.claims.clear();
        self.messages.clear();
    }

    pub fn history(&self, conversation: &ConversationId, limit: Option<usize>) -> Vec<T> {
        let messages = self
            .messages
            .get(conversation)
            .map(Vec::as_slice)
            .unwrap_or_default();
        // Python's messages[-0:] returns the entire history.
        let start = match limit {
            Some(limit) if limit > 0 => messages.len().saturating_sub(limit),
            _ => 0,
        };
        messages[start..].to_vec()
    }
}

/// Dropping this permit must release the conversation lock.
pub trait ConversationPermit: Send {}
impl ConversationPermit for OwnedMutexGuard<()> {}

#[async_trait]
pub trait State: Send + Sync {
    async fn claim(&self, message_id: &MessageId) -> Result<bool>;
    async fn acquire(
        &self,
        conversation_id: &ConversationId,
    ) -> Result<Box<dyn ConversationPermit>>;
    async fn append(&self, message: Message) -> Result<()>;
    async fn history(
        &self,
        conversation_id: &ConversationId,
        limit: Option<usize>,
    ) -> Result<Vec<Message>>;
}

#[derive(Default)]
pub struct MemoryState {
    store: Mutex<MemoryStore<Message>>,
    locks: Mutex<HashMap<ConversationId, Arc<Mutex<()>>>>,
}

impl MemoryState {
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait]
impl State for MemoryState {
    async fn claim(&self, message_id: &MessageId) -> Result<bool> {
        Ok(self.store.lock().await.claim(message_id))
    }

    async fn acquire(
        &self,
        conversation_id: &ConversationId,
    ) -> Result<Box<dyn ConversationPermit>> {
        let lock = self
            .locks
            .lock()
            .await
            .entry(conversation_id.clone())
            .or_default()
            .clone();
        Ok(Box::new(lock.lock_owned().await))
    }

    async fn append(&self, message: Message) -> Result<()> {
        self.store
            .lock()
            .await
            .append(&message.conversation_id.clone(), message);
        Ok(())
    }

    async fn history(
        &self,
        conversation_id: &ConversationId,
        limit: Option<usize>,
    ) -> Result<Vec<Message>> {
        Ok(self.store.lock().await.history(conversation_id, limit))
    }
}
