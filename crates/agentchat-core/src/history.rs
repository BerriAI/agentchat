use crate::{Error, Result};

/// Validated context-history bound, shared by runtimes and provider adapters.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HistoryLimit(usize);

impl HistoryLimit {
    pub fn new(limit: usize) -> Result<Self> {
        if !(2..=100).contains(&limit) {
            return Err(Error::InvalidInput(
                "History limit must be between 2 and 100".into(),
            ));
        }
        Ok(Self(limit))
    }

    pub fn get(self) -> usize {
        self.0
    }

    pub fn indices(self, length: usize) -> Vec<usize> {
        if length <= self.0 {
            return (0..length).collect();
        }
        std::iter::once(0)
            .chain(length - (self.0 - 1)..length)
            .collect()
    }

    pub fn select<T: Clone>(self, messages: &[T]) -> Vec<T> {
        self.indices(messages.len())
            .into_iter()
            .map(|index| messages[index].clone())
            .collect()
    }
}

/// Indices into provider history plus the current turn. Keep the root and tail.
/// Hosts use these indices without copying opaque language objects through Rust.
pub fn thread_history_indices(length: usize, limit: usize) -> Result<Vec<usize>> {
    Ok(HistoryLimit::new(limit)?.indices(length))
}

pub fn select_history<T: Clone>(messages: &[T], limit: usize) -> Result<Vec<T>> {
    Ok(HistoryLimit::new(limit)?.select(messages))
}
