//! The client of one session's daemon.

use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};

use serde::de::DeserializeOwned;
use tokio_util::sync::CancellationToken;

use crate::credential::{API_KEY_ENVIRONMENT, credential_type};
use crate::environment::Environment;
use crate::error::{Error, ErrorCode, Result};
use crate::failover::Failovers;
use crate::groups::{Backups, Databases, Events, Replicas, Rules, Session};
use crate::launch::launch_daemon;
use crate::paths::session_socket;
use crate::transport::{Answer, Transport, TransportFailure};
use crate::wire::{self, Call};

/// Configures and connects a [`Client`]; [`Client::builder`] makes one.
#[must_use]
#[derive(Clone, Default)]
pub struct ClientBuilder {
    api_key: Option<String>,
    session: Option<String>,
    socket_path: Option<PathBuf>,
    binary_path: Option<PathBuf>,
    no_launch: bool,
    environment: Option<Environment>,
}

impl fmt::Debug for ClientBuilder {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        // The credential and the environment can hold secrets.
        formatter
            .debug_struct("ClientBuilder")
            .field("api_key", &self.api_key.as_ref().map(|_| "<set>"))
            .field("session", &self.session)
            .field("socket_path", &self.socket_path)
            .field("binary_path", &self.binary_path)
            .field("launch", &!self.no_launch)
            .field("environment", &self.environment.as_ref().map(|_| "<set>"))
            .finish()
    }
}

impl ClientBuilder {
    /// The credential the daemon uses when it starts: an API key, or a join
    /// token it exchanges once for a key of this machine, then `@` and the
    /// server's address. It overrides `LITEBACKUP_API_KEY`, and an empty one
    /// is no credential. The daemon keeps the key it saved unless this holds a
    /// different credential, which the launch refuses with `drain_required`
    /// while the client holds write locks; a daemon already running keeps its
    /// key until it restarts. Foreground backups use it too.
    pub fn api_key(mut self, api_key: impl Into<String>) -> Self {
        self.api_key = Some(api_key.into());
        self
    }

    /// The session name; `LITEBACKUP_SESSION`, then `default`, without it.
    pub fn session(mut self, session: impl Into<String>) -> Self {
        self.session = Some(session.into());
        self
    }

    /// Connects to this socket and never starts a daemon.
    pub fn socket_path(mut self, socket_path: impl AsRef<Path>) -> Self {
        self.socket_path = Some(socket_path.as_ref().to_path_buf());
        self
    }

    /// The litebackup binary that starts the daemon and runs foreground
    /// backups; without it, `LITEBACKUP_PATH`, then `litebackup` on `PATH`,
    /// then `~/.local/bin/litebackup`.
    pub fn binary_path(mut self, binary_path: impl AsRef<Path>) -> Self {
        self.binary_path = Some(binary_path.as_ref().to_path_buf());
        self
    }

    /// Whether connecting starts the session's daemon when it is not
    /// running; it does unless this is false.
    pub fn launch(mut self, launch: bool) -> Self {
        self.no_launch = !launch;
        self
    }

    /// The environment the client resolves paths with and starts the daemon
    /// and foreground backups with, exactly, empty values included; this
    /// process's without it.
    pub fn environment<K, V>(mut self, variables: impl IntoIterator<Item = (K, V)>) -> Self
    where
        K: Into<std::ffi::OsString>,
        V: Into<std::ffi::OsString>,
    {
        self.environment = Some(Environment::from_variables(variables));
        self
    }

    /// Connects, starting the session's daemon like the `litebackup` command
    /// does when nothing listens on its socket. Fails with
    /// `daemon_unsupported` for a daemon older than
    /// [`MINIMUM_DAEMON_VERSION`](crate::MINIMUM_DAEMON_VERSION).
    ///
    /// # Errors
    ///
    /// `invalid_argument` for a malformed credential or session name,
    /// `binary_not_found`, `launch_failed`, or `drain_required` when the daemon
    /// could not be started, and `connection_failed` when nothing answers.
    #[cfg_attr(feature = "tracing", tracing::instrument(name = "litebackup.connect", skip_all))]
    pub async fn connect(self) -> Result<Client> {
        let environment = self.environment.unwrap_or_else(Environment::current);
        // Child commands get the option itself when there is one, so an
        // empty option clears an inherited LITEBACKUP_API_KEY.
        let api_key_variable = self.api_key.clone();
        let api_key = match self.api_key {
            Some(api_key) => Some(api_key).filter(|api_key| !api_key.is_empty()),
            None => environment.text(API_KEY_ENVIRONMENT).map(str::to_owned),
        };
        credential_type(api_key.as_deref())?;
        let explicit_socket = self.socket_path.is_some();
        let (session, socket_path) = match self.socket_path {
            Some(path) if path.as_os_str().is_empty() => {
                return Err(Error::invalid_argument("the socket path is empty"));
            }
            Some(path) => (self.session.filter(|session| !session.is_empty()), path),
            None => {
                let (session, path) = session_socket(self.session.as_deref(), &environment)?;
                (Some(session), path)
            }
        };
        let settings = Settings {
            environment,
            api_key_variable,
            session,
            socket_path,
            binary_path: self.binary_path,
            launch: !self.no_launch && !explicit_socket,
            explicit_socket,
        };
        if settings.launch
            && let Some(session) = &settings.session
        {
            launch_daemon(
                session,
                &settings.socket_path,
                &settings.environment,
                settings.api_key_variable.as_deref(),
                settings.binary_path.as_deref(),
            )
            .await?;
        }
        let client = Client {
            shared: Arc::new(ClientShared {
                transport: Transport::new(&settings.socket_path),
                settings,
                client_version: Mutex::new(String::new()),
                lifetime: CancellationToken::new(),
                owner_pid: std::process::id(),
            }),
        };
        let probe = Call::get("/v1/status/show", vec![("limit", "1".to_owned())]);
        let answer = client.exchange(&probe).await?;
        if answer.status != 200 {
            return Err(wire::response_error(answer.status, &answer.body));
        }
        wire::check_daemon_version(&client.client_version(), client.session_name())?;
        Ok(client)
    }
}

/// What a client was connected with.
#[derive(Debug)]
pub(crate) struct Settings {
    pub(crate) environment: Environment,
    /// What child commands get as `LITEBACKUP_API_KEY`: the client's option,
    /// even empty, or `None` to leave the environment's as it is.
    pub(crate) api_key_variable: Option<String>,
    pub(crate) session: Option<String>,
    pub(crate) socket_path: PathBuf,
    pub(crate) binary_path: Option<PathBuf>,
    launch: bool,
    pub(crate) explicit_socket: bool,
}

/// Talks to the Litebackup daemon of one session over its unix socket,
/// starting it like the `litebackup` command does:
///
/// ```no_run
/// # async fn example() -> litebackup::Result<()> {
/// let client = litebackup::Client::builder().connect().await?;
/// client
///     .backups()
///     .start("/var/lib/app/main.sqlite", litebackup::BackupOptions::default())
///     .await?;
/// client.close();
/// # Ok(())
/// # }
/// ```
///
/// A client is cheap to clone; clones share its connections. Closing it
/// ([`close`](Self::close)) never shuts the daemon down. A client belongs to
/// the process that connected it: in a forked child its calls fail with
/// `invalid_argument`, so connect a new client there.
#[derive(Clone)]
pub struct Client {
    shared: Arc<ClientShared>,
}

struct ClientShared {
    settings: Settings,
    transport: Transport,
    client_version: Mutex<String>,
    /// Cancelled by close, which ends calls in flight.
    lifetime: CancellationToken,
    owner_pid: u32,
}

impl fmt::Debug for Client {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Client")
            .field("session", &self.session_name())
            .field("socket_path", &self.socket_path())
            .field("closed", &self.is_closed())
            .finish_non_exhaustive()
    }
}

impl Client {
    /// A builder for a client of this process's session.
    pub fn builder() -> ClientBuilder {
        ClientBuilder::default()
    }

    /// The session name, or `None` for a client of an explicit socket path
    /// given no session.
    #[must_use]
    pub fn session_name(&self) -> Option<&str> {
        self.shared.settings.session.as_deref()
    }

    /// The daemon's socket.
    #[must_use]
    pub fn socket_path(&self) -> &Path {
        &self.shared.settings.socket_path
    }

    /// The daemon's version, from its last answer.
    #[must_use]
    pub fn client_version(&self) -> String {
        self.shared.client_version.lock().unwrap_or_else(PoisonError::into_inner).clone()
    }

    /// Whether [`close`](Self::close) ran.
    #[must_use]
    pub fn is_closed(&self) -> bool {
        self.shared.lifetime.is_cancelled()
    }

    /// Ends the client's calls in flight with `closed`, stops its event
    /// watches, failover followers, and foreground backups, and closes its
    /// connections; every clone shares the close. The daemon keeps running,
    /// and so do its failovers and acquired databases. Calls afterwards fail
    /// with `closed`.
    pub fn close(&self) {
        self.shared.lifetime.cancel();
        self.shared.transport.close_idle();
    }

    /// Continuous backups and one-shot backup jobs.
    #[must_use]
    pub fn backups(&self) -> Backups<'_> {
        Backups::new(self)
    }

    /// The backup and failover rules on this machine.
    #[must_use]
    pub fn rules(&self) -> Rules<'_> {
        Rules::new(self)
    }

    /// Databases: status, versions, restores, acquires, and pools.
    #[must_use]
    pub fn databases(&self) -> Databases<'_> {
        Databases::new(self)
    }

    /// Live readonly replicas.
    #[must_use]
    pub fn replicas(&self) -> Replicas<'_> {
        Replicas::new(self)
    }

    /// The daemon's event log.
    #[must_use]
    pub fn events(&self) -> Events<'_> {
        Events::new(self)
    }

    /// The session: its status and its shutdown.
    #[must_use]
    pub fn session(&self) -> Session<'_> {
        Session::new(self)
    }

    /// Failovers: one database on several identical machines.
    #[must_use]
    pub fn failovers(&self) -> Failovers<'_> {
        Failovers::new(self)
    }

    pub(crate) fn settings(&self) -> &Settings {
        &self.shared.settings
    }

    /// Cancelled once the client closes.
    pub(crate) fn lifetime(&self) -> &CancellationToken {
        &self.shared.lifetime
    }

    /// Fails with `closed` after close and `invalid_argument` in a forked child.
    pub(crate) fn check_open(&self) -> Result<()> {
        if self.is_closed() {
            return Err(Error::closed());
        }
        if self.shared.owner_pid != std::process::id() {
            return Err(Error::invalid_argument(format!(
                "this client was connected in process {} and cannot be used after a fork; \
                 connect a new client in this process",
                self.shared.owner_pid
            )));
        }
        Ok(())
    }

    /// Sends a call and decodes its answer.
    pub(crate) async fn call<T: DeserializeOwned>(&self, call: Call) -> Result<T> {
        let answer = self.exchange(&call).await?;
        if answer.status != 200 {
            return Err(wire::response_error(answer.status, &answer.body));
        }
        wire::decode_answer(call.path, &answer.body)
    }

    /// Sends a call and reads its whole answer, within the call's deadline,
    /// ending with `closed` once the client closes.
    async fn exchange(&self, call: &Call) -> Result<Answer> {
        self.check_open()?;
        let body = call.body.as_ref().map(|body| serde_json::Value::Object(body.clone()).to_string().into_bytes());
        let target = call.target();
        let request = self.shared.transport.request(call.method, &target, body);
        let within_deadline = async {
            let answer = match call.deadline() {
                Some(deadline) => tokio::time::timeout(deadline, request).await.map_err(|_| {
                    Error::new(
                        ErrorCode::TIMEOUT,
                        "the daemon did not answer within the call's timeout; it may still finish the operation",
                    )
                })?,
                None => request.await,
            };
            answer.map_err(|failure| match failure {
                TransportFailure::Connection(message) => Error::new(ErrorCode::CONNECTION_FAILED, message),
                TransportFailure::TooLarge => {
                    Error::new(ErrorCode::INVALID_RESPONSE, "the daemon's answer is too large")
                }
            })
        };
        let answer = tokio::select! {
            biased;
            () = self.shared.lifetime.cancelled() => return Err(Error::closed()),
            answer = within_deadline => answer?,
        };
        self.shared.client_version.lock().unwrap_or_else(PoisonError::into_inner).clone_from(&answer.client_version);
        Ok(answer)
    }
}
