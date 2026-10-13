use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

macro_rules! identifier {
    ($name:ident) => {
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub String);

        impl From<String> for $name {
            fn from(value: String) -> Self {
                Self(value)
            }
        }
        impl From<&str> for $name {
            fn from(value: &str) -> Self {
                Self(value.to_owned())
            }
        }
        impl AsRef<str> for $name {
            fn as_ref(&self) -> &str {
                &self.0
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(f)
            }
        }
    };
}

identifier!(MessageId);
identifier!(ConversationId);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Sender {
    pub id: String,
    #[serde(default)]
    pub display_name: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Attachment {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub media_type: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Message {
    pub id: MessageId,
    pub conversation_id: ConversationId,
    pub channel: String,
    pub sender: Sender,
    pub text: String,
    pub role: Role,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    #[serde(default)]
    pub thread_id: Option<String>,
    #[serde(default = "addressed_by_default")]
    pub addressed: bool,
    #[serde(default)]
    pub attachments: Vec<Attachment>,
}

fn addressed_by_default() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RichReply {
    pub text: String,
    #[serde(default)]
    pub blocks: Vec<Map<String, Value>>,
    #[serde(default)]
    pub attachments: Vec<Map<String, Value>>,
}

#[derive(Clone, PartialEq, Eq)]
pub struct UploadFile {
    pub filename: String,
    pub content: Vec<u8>,
    pub title: Option<String>,
}

impl fmt::Debug for UploadFile {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("UploadFile")
            .field("filename", &self.filename)
            .field("content_length", &self.content.len())
            .field("title", &self.title)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadedFile {
    pub id: String,
    pub permalink: Option<String>,
}

#[derive(Clone, PartialEq, Eq)]
pub struct DownloadedFile {
    pub id: String,
    pub name: String,
    pub content: Vec<u8>,
    pub media_type: Option<String>,
}

impl fmt::Debug for DownloadedFile {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DownloadedFile")
            .field("id", &self.id)
            .field("name", &self.name)
            .field("content_length", &self.content.len())
            .field("media_type", &self.media_type)
            .finish_non_exhaustive()
    }
}
