#![allow(dead_code)]

use std::collections::{HashMap, VecDeque};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use std::time::Duration;

use agentchat_slack::{Slack, SlackConfig};
use axum::{
    body::Bytes,
    extract::{
        ws::{Message, WebSocket},
        Path, State, WebSocketUpgrade,
    },
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{any, get},
    Router,
};
use futures_util::StreamExt;
use serde_json::{json, Value};
use tokio::sync::{mpsc, Mutex, Notify};
use tokio::task::JoinHandle;

#[derive(Clone, Debug)]
pub struct Request {
    pub method: String,
    pub payload: Value,
    pub authorization: Option<String>,
    pub query: Option<String>,
}

#[derive(Clone)]
pub enum Reply {
    Json(StatusCode, Value),
    Gate(Arc<Notify>, Value),
    Bytes(Vec<u8>),
    Redirect(String),
}

#[derive(Clone)]
pub struct MockState {
    pub base: String,
    pub requests: Arc<Mutex<Vec<Request>>>,
    pub overrides: Arc<Mutex<HashMap<String, VecDeque<Reply>>>>,
    pub connections: Arc<AtomicUsize>,
    pub inbound: Arc<Mutex<mpsc::UnboundedReceiver<Message>>>,
    pub acks: mpsc::UnboundedSender<Value>,
}

pub struct MockSlack {
    pub state: MockState,
    pub sender: mpsc::UnboundedSender<Message>,
    acks: Mutex<mpsc::UnboundedReceiver<Value>>,
    server: JoinHandle<()>,
}

impl Drop for MockSlack {
    fn drop(&mut self) {
        self.server.abort();
    }
}

impl MockSlack {
    pub async fn start() -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let (sender, inbound) = mpsc::unbounded_channel();
        let (acks_sender, acks) = mpsc::unbounded_channel();
        let state = MockState {
            base,
            requests: Arc::new(Mutex::new(Vec::new())),
            overrides: Arc::new(Mutex::new(HashMap::new())),
            connections: Arc::new(AtomicUsize::new(0)),
            inbound: Arc::new(Mutex::new(inbound)),
            acks: acks_sender,
        };
        let app = Router::new()
            .route("/api/{method}", any(api))
            .route("/socket", get(socket_upgrade))
            .route("/upload/{id}", any(upload))
            .route("/files-pri/{id}", get(download))
            .with_state(state.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self {
            state,
            sender,
            acks: Mutex::new(acks),
            server,
        }
    }

    pub fn config(&self) -> SlackConfig {
        let mut config = SlackConfig::new("xoxb-test-secret".into(), "xapp-test-secret".into());
        config.api_base_url = format!("{}/api/", self.state.base);
        config.ack_emoji = None;
        config.shutdown_timeout = Duration::from_millis(100);
        config
    }
    pub fn slack(&self) -> Slack {
        Slack::new(self.config()).unwrap()
    }
    pub async fn override_reply(&self, method: &str, reply: Reply) {
        self.state
            .overrides
            .lock()
            .await
            .entry(method.into())
            .or_default()
            .push_back(reply);
    }
    pub async fn calls(&self, method: &str) -> Vec<Request> {
        self.state
            .requests
            .lock()
            .await
            .iter()
            .filter(|request| request.method == method)
            .cloned()
            .collect()
    }
    pub fn send(&self, value: Value) {
        self.sender
            .send(Message::Text(value.to_string().into()))
            .unwrap();
    }
    pub async fn ack(&self) -> Value {
        tokio::time::timeout(Duration::from_secs(3), self.acks.lock().await.recv())
            .await
            .unwrap()
            .unwrap()
    }
    pub async fn connected(&self, count: usize) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while self.state.connections.load(Ordering::Acquire) < count {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
    }
}

pub fn event(timestamp: &str) -> Value {
    json!({"team_id":"T1","event_id":format!("Ev{timestamp}"),"event":{
        "type":"app_mention","channel":"C1","user":"U1","ts":timestamp,"text":"<@BOT> hello"}})
}

pub fn envelope(id: &str, timestamp: &str) -> Value {
    json!({"type":"events_api","envelope_id":id,"payload":event(timestamp)})
}

async fn api(
    State(state): State<MockState>,
    Path(method): Path<String>,
    headers: HeaderMap,
    uri: Uri,
    body: Bytes,
) -> Response {
    let payload = serde_json::from_slice(&body)
        .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&body).into_owned()));
    state.requests.lock().await.push(Request {
        method: method.clone(),
        payload,
        authorization: headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned),
        query: uri.query().map(str::to_owned),
    });
    let response = state
        .overrides
        .lock()
        .await
        .get_mut(&method)
        .and_then(VecDeque::pop_front);
    if let Some(response) = response {
        return render(response).await;
    }
    let value = match method.as_str() {
        "auth.test" => json!({"ok":true,"team_id":"T1","user_id":"BOT"}),
        "apps.connections.open" => {
            json!({"ok":true,"url":format!("{}/socket", state.base.replace("http://", "ws://"))})
        }
        "chat.postMessage" => json!({"ok":true,"ts":"9.000001"}),
        "files.getUploadURLExternal" => {
            json!({"ok":true,"file_id":"F1","upload_url":format!("{}/upload/F1",state.base)})
        }
        "files.completeUploadExternal" => {
            json!({"ok":true,"files":[{"id":"F1","permalink":"https://slack.com/file/F1"}]})
        }
        "files.info" => {
            json!({"ok":true,"file":{"id":"F1","size":4,"name":"test.txt","mimetype":"text/plain",
            "url_private_download":format!("{}/files-pri/F1",state.base)}})
        }
        "conversations.replies" => json!({"ok":true,"messages":[],"has_more":false}),
        _ => json!({"ok":true}),
    };
    axum::Json(value).into_response()
}

async fn render(reply: Reply) -> Response {
    match reply {
        Reply::Json(status, value) => (status, axum::Json(value)).into_response(),
        Reply::Gate(gate, value) => {
            gate.notified().await;
            axum::Json(value).into_response()
        }
        Reply::Bytes(bytes) => bytes.into_response(),
        Reply::Redirect(url) => (StatusCode::FOUND, [("location", url)]).into_response(),
    }
}

async fn upload(State(state): State<MockState>, headers: HeaderMap, body: Bytes) -> Response {
    state.requests.lock().await.push(Request {
        method: "upload".into(),
        payload: json!(body.to_vec()),
        authorization: headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned),
        query: None,
    });
    "OK".into_response()
}

async fn download(State(state): State<MockState>, headers: HeaderMap) -> Response {
    state.requests.lock().await.push(Request {
        method: "download".into(),
        payload: Value::Null,
        authorization: headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned),
        query: None,
    });
    let response = state
        .overrides
        .lock()
        .await
        .get_mut("download")
        .and_then(VecDeque::pop_front);
    render(response.unwrap_or(Reply::Bytes(b"test".to_vec()))).await
}

async fn socket_upgrade(State(state): State<MockState>, upgrade: WebSocketUpgrade) -> Response {
    upgrade.on_upgrade(move |socket| socket_session(state, socket))
}

async fn socket_session(state: MockState, mut socket: WebSocket) {
    state.connections.fetch_add(1, Ordering::AcqRel);
    let _ = socket
        .send(Message::Text(json!({"type":"hello"}).to_string().into()))
        .await;
    loop {
        tokio::select! {
            message = async { state.inbound.lock().await.recv().await } => {
                let Some(message) = message else { return };
                if socket.send(message).await.is_err() { return; }
            }
            message = socket.next() => match message {
                Some(Ok(Message::Text(text))) => { let _ = state.acks.send(serde_json::from_str(&text).unwrap()); }
                Some(Ok(Message::Ping(bytes))) => {
                    let response = socket.send(Message::Pong(bytes)).await;
                    if response.is_err() { return; }
                }
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => return,
                _ => {}
            }
        }
    }
}
