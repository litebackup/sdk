//! HTTP/1.1 over the daemon's unix socket, with a small keep-alive pool.
//!
//! Each connection is a hyper `http1` connection on a [`UnixStream`], driven
//! by a task of its own. A GET that fails on a reused connection before any
//! answer arrives is sent once more, on a new connection; nothing else is
//! ever sent twice. The caller bounds the whole call, connecting, the retry,
//! the answer's head, and its whole body, with one deadline.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::client::conn::http1::{self, SendRequest};
use hyper::header::{CONNECTION, CONTENT_TYPE, HOST};
use hyper::{Request, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::UnixStream;

use crate::wire::{CLIENT_VERSION_HEADER, Method, RESPONSE_LIMIT, SDK_VERSION_HEADER, sdk_version_value};

/// How many idle connections the pool keeps.
const POOL_SIZE: usize = 4;

/// One whole answer.
#[derive(Debug)]
pub(crate) struct Answer {
    pub(crate) status: u16,
    /// The daemon's version, empty when the answer has none.
    pub(crate) client_version: String,
    pub(crate) body: Bytes,
}

/// Why a call ended without a whole answer.
#[derive(Debug)]
pub(crate) enum TransportFailure {
    /// The socket refused the connection, or the connection broke.
    Connection(String),
    /// The answer's body is over [`RESPONSE_LIMIT`].
    TooLarge,
}

/// The connections of one client to one socket.
#[derive(Debug)]
pub(crate) struct Transport {
    socket_path: PathBuf,
    idle: Mutex<Vec<SendRequest<Full<Bytes>>>>,
}

impl Transport {
    pub(crate) fn new(socket_path: &Path) -> Self {
        Self { socket_path: socket_path.to_path_buf(), idle: Mutex::new(Vec::new()) }
    }

    /// Drops the idle connections, which ends their tasks.
    pub(crate) fn close_idle(&self) {
        self.idle_connections().clear();
    }

    fn idle_connections(&self) -> std::sync::MutexGuard<'_, Vec<SendRequest<Full<Bytes>>>> {
        // A panic while the lock was held leaves only a list of senders.
        self.idle.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Sends one request and reads its whole answer.
    pub(crate) async fn request(
        &self,
        method: Method,
        target: &str,
        body: Option<Vec<u8>>,
    ) -> Result<Answer, TransportFailure> {
        let mut retried = false;
        loop {
            let (mut sender, reused) = match self.idle_sender(retried) {
                Some(sender) => (sender, true),
                None => (self.connect().await?, false),
            };
            let request = build_request(method, target, body.clone())?;
            let response = match sender.send_request(request).await {
                Ok(response) => response,
                // No answer arrived: a GET on a reused connection, which the
                // daemon may have closed while it was idle, goes once more on
                // a new one.
                Err(error) if reused && !retried && method == Method::Get => {
                    drop(error);
                    retried = true;
                    continue;
                }
                Err(error) => return Err(TransportFailure::Connection(error.to_string())),
            };
            let status = response.status();
            let client_version = response
                .headers()
                .get(CLIENT_VERSION_HEADER)
                .and_then(|value| value.to_str().ok())
                .unwrap_or_default()
                .to_owned();
            let keep_alive = !response
                .headers()
                .get(CONNECTION)
                .and_then(|value| value.to_str().ok())
                .is_some_and(|value| value.eq_ignore_ascii_case("close"));
            let body = Limited::new(response.into_body(), RESPONSE_LIMIT).collect().await.map_err(|error| {
                if error.is::<http_body_util::LengthLimitError>() {
                    TransportFailure::TooLarge
                } else {
                    TransportFailure::Connection(format!("the daemon closed the connection mid-answer: {error}"))
                }
            })?;
            if keep_alive {
                self.return_sender(sender);
            }
            return Ok(Answer { status: status_code(status), client_version, body: body.to_bytes() });
        }
    }

    /// An idle connection that is still open, unless a new one is wanted.
    fn idle_sender(&self, fresh: bool) -> Option<SendRequest<Full<Bytes>>> {
        if fresh {
            return None;
        }
        let mut idle = self.idle_connections();
        while let Some(sender) = idle.pop() {
            if !sender.is_closed() && sender.is_ready() {
                return Some(sender);
            }
        }
        None
    }

    fn return_sender(&self, sender: SendRequest<Full<Bytes>>) {
        let mut idle = self.idle_connections();
        if idle.len() < POOL_SIZE && !sender.is_closed() {
            idle.push(sender);
        }
    }

    async fn connect(&self) -> Result<SendRequest<Full<Bytes>>, TransportFailure> {
        let stream = UnixStream::connect(&self.socket_path).await.map_err(|error| {
            TransportFailure::Connection(format!("connecting to {} failed: {error}", self.socket_path.display()))
        })?;
        let (sender, connection) = http1::handshake(TokioIo::new(stream)).await.map_err(|error| {
            TransportFailure::Connection(format!("connecting to {} failed: {error}", self.socket_path.display()))
        })?;
        // The connection's task ends when the daemon closes it or every
        // sender is dropped.
        tokio::spawn(async move {
            let _ = connection.await;
        });
        Ok(sender)
    }
}

fn status_code(status: StatusCode) -> u16 {
    status.as_u16()
}

fn build_request(
    method: Method,
    target: &str,
    body: Option<Vec<u8>>,
) -> Result<Request<Full<Bytes>>, TransportFailure> {
    let mut builder = Request::builder()
        .method(method.as_str())
        .uri(target)
        .header(HOST, "litebackup")
        .header(SDK_VERSION_HEADER, sdk_version_value());
    if body.is_some() {
        builder = builder.header(CONTENT_TYPE, "application/json");
    }
    builder
        .body(Full::new(body.map(Bytes::from).unwrap_or_default()))
        .map_err(|error| TransportFailure::Connection(format!("building the request to {target} failed: {error}")))
}
