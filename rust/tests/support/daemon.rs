//! A fake daemon in the test process: a hyper server on a unix socket in a
//! temporary directory that answers scripted calls and records each request
//! with the connection it came on.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::net::UnixListener;

/// One scripted answer: the request it matches and what it answers.
#[derive(Debug, Clone)]
pub struct Answer {
    pub path: String,
    /// Matches only a request with exactly this query, when set.
    pub query: Option<BTreeMap<String, String>>,
    pub status: u16,
    pub content_type: String,
    pub body: String,
    /// How many requests it answers; 0 answers every one.
    pub uses: usize,
    /// How long it waits before answering.
    pub delay: Duration,
    /// Closes the connection without answering.
    pub hang_up: bool,
}

impl Answer {
    /// A 200 JSON answer to every request for path.
    pub fn json(path: &str, body: &Value) -> Self {
        Self::status(path, 200, body)
    }

    /// A JSON answer with a status to every request for path.
    pub fn status(path: &str, status: u16, body: &Value) -> Self {
        Self {
            path: path.to_owned(),
            query: None,
            status,
            content_type: "application/json".to_owned(),
            body: body.to_string(),
            uses: 0,
            delay: Duration::ZERO,
            hang_up: false,
        }
    }

    /// A raw answer with its content type, as a proxy might send.
    pub fn text(path: &str, status: u16, content_type: &str, body: &str) -> Self {
        Self {
            content_type: content_type.to_owned(),
            body: body.to_owned(),
            ..Self::status(path, status, &Value::Null)
        }
    }

    pub fn uses(mut self, uses: usize) -> Self {
        self.uses = uses;
        self
    }

    pub fn query(mut self, query: BTreeMap<String, String>) -> Self {
        self.query = Some(query);
        self
    }

    pub fn delay(mut self, delay: Duration) -> Self {
        self.delay = delay;
        self
    }

    pub fn hang_up(mut self) -> Self {
        self.hang_up = true;
        self
    }
}

/// A request the daemon received.
#[derive(Debug, Clone)]
pub struct Received {
    pub method: String,
    pub path: String,
    pub query: BTreeMap<String, String>,
    /// The JSON body, or null without one.
    pub body: Value,
    /// The Litebackup-SDK-Version header.
    pub sdk_version: String,
    /// Which connection, counted from 0, carried it.
    pub connection: usize,
    pub arrived: Instant,
}

impl Received {
    /// Whether this is the connect probe every client sends first.
    pub fn probe(&self) -> bool {
        self.path == "/v1/status/show"
            && self.query.len() == 1
            && self.query.get("limit").map(String::as_str) == Some("1")
    }
}

#[derive(Debug)]
struct DaemonState {
    answers: Vec<(Answer, usize)>,
    received: Vec<Received>,
    connections: usize,
    client_version: Option<String>,
}

/// The fake daemon; dropping it stops it and removes its directory.
pub struct FakeDaemon {
    pub socket_path: PathBuf,
    state: Arc<Mutex<DaemonState>>,
    server: tokio::task::JoinHandle<()>,
    /// The temporary directory the socket is in, removed with the daemon.
    directory: Option<tempfile::TempDir>,
}

impl Drop for FakeDaemon {
    fn drop(&mut self) {
        self.server.abort();
    }
}

impl FakeDaemon {
    /// Starts a daemon on a socket in a new temporary directory.
    pub fn start(answers: Vec<Answer>) -> Self {
        let directory = tempfile::tempdir().expect("a temporary directory");
        let socket_path = directory.path().join("client.sock");
        Self::start_in(&socket_path, answers, Some(directory))
    }

    /// Starts a daemon on a socket path, creating its directory.
    pub fn start_on(socket_path: &Path, answers: Vec<Answer>) -> Self {
        Self::start_in(socket_path, answers, None)
    }

    fn start_in(socket_path: &Path, answers: Vec<Answer>, directory: Option<tempfile::TempDir>) -> Self {
        if let Some(parent) = socket_path.parent() {
            std::fs::create_dir_all(parent).expect("the socket's directory");
        }
        let listener = UnixListener::bind(socket_path).expect("binding the fake daemon's socket");
        let state = Arc::new(Mutex::new(DaemonState {
            answers: answers.into_iter().map(|answer| (answer, 0)).collect(),
            received: Vec::new(),
            connections: 0,
            client_version: Some(litebackup::VERSION.to_owned()),
        }));
        let server_state = Arc::clone(&state);
        let server = tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let connection = {
                    let mut state = server_state.lock().expect("the daemon's state");
                    state.connections += 1;
                    state.connections - 1
                };
                let request_state = Arc::clone(&server_state);
                tokio::spawn(async move {
                    let service = service_fn(move |request| answer(Arc::clone(&request_state), connection, request));
                    let _ = http1::Builder::new().serve_connection(TokioIo::new(stream), service).await;
                });
            }
        });
        Self { socket_path: socket_path.to_path_buf(), state, server, directory }
    }

    /// The daemon version answers carry; none leaves the header out.
    pub fn set_client_version(&self, version: Option<&str>) {
        self.state.lock().expect("the daemon's state").client_version = version.map(str::to_owned);
    }

    /// Adds answers after the scripted ones.
    pub fn add(&self, answer: Answer) {
        self.state.lock().expect("the daemon's state").answers.push((answer, 0));
    }

    /// Every request but the connect probes, in the order they arrived.
    pub fn requests(&self) -> Vec<Received> {
        self.all_requests().into_iter().filter(|request| !request.probe()).collect()
    }

    /// Every request, the connect probes included.
    pub fn all_requests(&self) -> Vec<Received> {
        self.state.lock().expect("the daemon's state").received.clone()
    }

    /// How many connections clients opened.
    pub fn connections(&self) -> usize {
        self.state.lock().expect("the daemon's state").connections
    }
}

type Answered = Result<Response<Full<Bytes>>, std::io::Error>;

async fn answer(state: Arc<Mutex<DaemonState>>, connection: usize, request: Request<Incoming>) -> Answered {
    let method = request.method().as_str().to_owned();
    let path = request.uri().path().to_owned();
    let query: BTreeMap<String, String> = request
        .uri()
        .query()
        .map(|query| {
            query
                .split('&')
                .filter(|pair| !pair.is_empty())
                .map(|pair| {
                    let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
                    (percent_decode(key), percent_decode(value))
                })
                .collect()
        })
        .unwrap_or_default();
    let sdk_version = request
        .headers()
        .get("litebackup-sdk-version")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    let body_bytes = request.into_body().collect().await.map(http_body_util::Collected::to_bytes).unwrap_or_default();
    let body = serde_json::from_slice(&body_bytes).unwrap_or(Value::Null);
    let received = Received { method, path, query, body, sdk_version, connection, arrived: Instant::now() };
    let (scripted, client_version) = {
        let mut state = state.lock().expect("the daemon's state");
        state.received.push(received.clone());
        let client_version = state.client_version.clone();
        let scripted = if received.probe() {
            Some(Answer::json("/v1/status/show", &serde_json::json!({"session": "default", "databases": []})))
        } else {
            state
                .answers
                .iter_mut()
                .find(|(answer, used)| {
                    answer.path == received.path
                        && answer.query.as_ref().is_none_or(|query| *query == received.query)
                        && (answer.uses == 0 || *used < answer.uses)
                })
                .map(|(answer, used)| {
                    *used += 1;
                    answer.clone()
                })
        };
        (scripted, client_version)
    };
    let scripted = scripted.unwrap_or_else(|| {
        Answer::status(
            &received.path,
            599,
            &serde_json::json!({"code": "unexpected_request", "message": format!("no answer for {} {:?}", received.path, received.query)}),
        )
    });
    if !scripted.delay.is_zero() {
        tokio::time::sleep(scripted.delay).await;
    }
    if scripted.hang_up {
        return Err(std::io::Error::other("the fake daemon hangs up"));
    }
    let mut response = Response::builder().status(scripted.status).header("content-type", scripted.content_type);
    if let Some(version) = client_version {
        response = response.header("litebackup-client-version", version);
    }
    Ok(response.body(Full::new(Bytes::from(scripted.body))).expect("a response"))
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("00");
                decoded.push(u8::from_str_radix(hex, 16).unwrap_or(b'?'));
                index += 3;
            }
            b'+' => {
                decoded.push(b' ');
                index += 1;
            }
            byte => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}
