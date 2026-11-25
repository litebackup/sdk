//! Against a real server and daemons: set `LITEBACKUP_INTEGRATION=1` and
//! `LITEBACKUP_PATH` to a static litebackup binary from `scripts/build`, as
//! `scripts/test --integration` does. The server runs `--development` with a
//! local bucket, as the Python, Go, TypeScript, Ruby, and PHP suites use.
#![allow(clippy::expect_used, clippy::unwrap_used, reason = "tests assert with expect")]
#![cfg(feature = "rusqlite")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use futures_util::StreamExt as _;
use litebackup::integrations::rusqlite::{RusqliteAdapter, RusqliteConnection};
use litebackup::{
    AcquireOptions, Adapter, BackupOptions, BoxError, Client, CloseError, Durability, ErrorCode, EventsOptions,
    FailoverNotice, FailoverOptions, LeaseOptions, PoolOptions, ReleaseOptions, ReplicaOptions, RestoreOptions, Role,
    ShutdownOptions, VersionsOptions,
};

/// A development server, its data, and the sessions' environment, removed
/// when dropped.
struct Server {
    binary: PathBuf,
    directory: tempfile::TempDir,
    tag: String,
    address: String,
    environment: Vec<(String, String)>,
    /// The server process.
    child: Child,
    clients: Vec<Client>,
}

impl Server {
    /// Starts a server, or returns `None` outside an integration run.
    async fn start() -> Option<Self> {
        let binary = std::env::var("LITEBACKUP_PATH").ok().filter(|path| !path.is_empty())?;
        if std::env::var("LITEBACKUP_INTEGRATION").as_deref() != Ok("1") {
            return None;
        }
        let directory = tempfile::Builder::new().prefix("lbrs").tempdir().unwrap();
        // Session names are lowercase.
        let tag = directory.path().file_name().unwrap().to_string_lossy().to_lowercase();
        let free_address = || std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().to_string();
        let address = free_address();
        // Each server on one host answers health checks on its own address.
        let health_address = free_address();
        let root = directory.path().display().to_string();
        let mut environment: Vec<(String, String)> = std::env::vars()
            .filter(|(variable, _)| {
                !["XDG_RUNTIME_DIR", "XDG_STATE_HOME", "LITEBACKUP_API_KEY", "LITEBACKUP_SESSION"]
                    .contains(&variable.as_str())
            })
            .collect();
        environment.extend([
            ("XDG_RUNTIME_DIR".to_owned(), format!("{root}/run")),
            ("XDG_STATE_HOME".to_owned(), format!("{root}/state")),
            ("LITEBACKUP_API_KEY".to_owned(), String::new()),
            ("LITEBACKUP_SESSION".to_owned(), String::new()),
        ]);
        let log = std::fs::File::create(directory.path().join("server.log")).unwrap();
        let child = Command::new(&binary)
            .args(["server", "run", "--development", "--data-directory", &format!("{root}/server")])
            .args(["--listen-address", &address, "--archive-delay", "1s", "--minimum-sync-interval", "0s"])
            .args(["--session-lease", "3s", "--session-heartbeat", "1s", "--health-address", &health_address])
            .env_clear()
            .envs(environment.iter().cloned())
            .stdin(Stdio::null())
            .stdout(log.try_clone().unwrap())
            .stderr(log)
            .spawn()
            .unwrap();
        let started =
            Self { binary: PathBuf::from(binary), directory, tag, address, environment, child, clients: Vec::new() };
        eventually(Duration::from_secs(30), || started.join_token("probe")).await;
        let bucket = format!("file://{root}/bucket");
        started.admin(&[
            "bucket",
            "create",
            "--provider",
            "s3",
            "--endpoint",
            &bucket,
            "--bucket",
            "bucket",
            "--name",
            "local",
        ]);
        started.admin(&["setup", "complete", "key_backup"]);
        started.admin(&["setup", "complete", "retention"]);
        Some(started)
    }

    fn admin(&self, arguments: &[&str]) -> String {
        let data = format!("{}/server", self.directory.path().display());
        let output = Command::new(&self.binary)
            .arg("server")
            .args(arguments)
            .args(["--data-directory", &data])
            .env_clear()
            .envs(self.environment.iter().cloned())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "litebackup server {arguments:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8_lossy(&output.stdout).into_owned()
    }

    fn join_token(&self, name: &str) -> Result<String, String> {
        let data = format!("{}/server", self.directory.path().display());
        let output = Command::new(&self.binary)
            .args([
                "server",
                "join-token",
                "create",
                "--name",
                name,
                "--stdout-format",
                "json",
                "--data-directory",
                &data,
            ])
            .env_clear()
            .envs(self.environment.iter().cloned())
            .output()
            .map_err(|error| error.to_string())?;
        let created: serde_json::Value = serde_json::from_slice(&output.stdout).map_err(|error| error.to_string())?;
        let token = created["token"].as_str().ok_or("no token yet")?;
        Ok(format!("{token}@http://{}", self.address))
    }

    /// A session of this run; its name carries the tag, so stray daemons are found.
    fn session(&self, name: &str) -> String {
        format!("{name}-{}", self.tag)
    }

    /// A client of a new session that joins with its own join token, as an
    /// app on another machine would.
    async fn connect(&mut self, name: &str) -> Client {
        let session = self.session(name);
        let token = self.join_token(&session).unwrap();
        let client = Client::builder()
            .session(&session)
            .api_key(token)
            .environment(self.environment.iter().cloned())
            .connect()
            .await
            .unwrap();
        self.clients.push(client.clone());
        client
    }

    /// A client of a session that joined before; its daemon keeps its join.
    async fn reconnect(&mut self, name: &str) -> Client {
        let client = Client::builder()
            .session(self.session(name))
            .environment(self.environment.iter().cloned())
            .connect()
            .await
            .unwrap();
        self.clients.push(client.clone());
        client
    }

    /// Stops a session's daemon with SIGKILL, so its lease runs out on the server.
    fn kill(&self, name: &str) {
        let wanted = format!("daemon --session {}", self.session(name));
        for pid in pids(|command| command.ends_with(&wanted)) {
            let _ = Command::new("kill").args(["-KILL", &pid.to_string()]).status();
        }
    }

    fn path(&self, elements: &[&str]) -> PathBuf {
        let path = elements.iter().fold(self.directory.path().to_path_buf(), |path, element| path.join(element));
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        path
    }

    /// Stops the daemons before the server, because their shutdown ends
    /// their sessions on it.
    async fn stop(mut self) {
        for client in std::mem::take(&mut self.clients) {
            let _ = client.session().shutdown(ShutdownOptions::default().durability(Durability::NONE)).await;
            client.close();
        }
    }
}

impl Drop for Server {
    /// Kills the daemons of this run and the server, also when a test panics.
    fn drop(&mut self) {
        let tag = self.tag.clone();
        for pid in pids(|command| command.contains(&tag) && command.contains("daemon --session")) {
            let _ = Command::new("kill").args(["-KILL", &pid.to_string()]).status();
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The PIDs whose command line matches, read from /proc.
fn pids(matches: impl Fn(&str) -> bool) -> Vec<u32> {
    std::fs::read_dir("/proc")
        .unwrap()
        .filter_map(|entry| {
            let pid: u32 = entry.ok()?.file_name().to_str()?.parse().ok()?;
            let command = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
            let command = String::from_utf8_lossy(&command).replace('\0', " ").trim().to_owned();
            matches(&command).then_some(pid)
        })
        .collect()
}

/// Calls the check until it succeeds or the time passes.
async fn eventually<T, E: std::fmt::Debug>(within: Duration, mut check: impl FnMut() -> Result<T, E>) -> T {
    let deadline = Instant::now() + within;
    loop {
        match check() {
            Ok(value) => return value,
            Err(error) if Instant::now() > deadline => panic!("never true: {error:?}"),
            Err(_) => tokio::time::sleep(Duration::from_millis(250)).await,
        }
    }
}

fn add_items(path: &Path, count: usize) {
    let database = rusqlite::Connection::open(path).unwrap();
    database.execute_batch("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body BLOB)").unwrap();
    for _ in 0..count {
        database.execute_batch("INSERT INTO items (body) VALUES (randomblob(2000))").unwrap();
    }
}

fn count_items(path: &Path) -> i64 {
    let database = rusqlite::Connection::open(path).unwrap();
    database.query_row("SELECT count(*) FROM items", [], |columns| columns.get(0)).unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn backs_up_registers_again_restores_replicates_acquires_and_releases() {
    let Some(mut server) = Server::start().await else {
        eprintln!("skipping: set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH");
        return;
    };
    let web1 = server.connect("web-1").await;
    let web2 = server.connect("web-2").await;
    let reader = server.connect("reader").await;
    assert_ne!(web1.client_version(), "");

    let main_path = server.path(&["web-1", "main.db"]);
    add_items(&main_path, 10);
    let rule = web1.backups().start(&main_path, BackupOptions::default()).await.unwrap();
    assert_eq!(rule.name, "main.db");
    assert_eq!(web1.backups().start(&main_path, BackupOptions::default()).await.unwrap().id, rule.id);
    let changed = web1.backups().start(&main_path, BackupOptions::default().maximum_local_databases(4)).await;
    let changed = changed.expect_err("changed settings were saved");
    assert_eq!(changed.code(), ErrorCode::RULE_EXISTS);
    assert_eq!(changed.details()["rule"]["id"], rule.id.as_str());
    web1.databases().sync(&rule.name).await.unwrap();
    add_items(&main_path, 5);
    let synced = web1.databases().sync(&rule.name).await.unwrap();
    let versions = web1.databases().versions("main.db", VersionsOptions::default()).await.unwrap();
    assert!(versions.len() >= 2);
    assert_eq!(versions.last().unwrap().version, synced.synced_server.version);
    let restored_path = server.path(&["restored", "main.db"]);
    let restored = web2
        .databases()
        .restore("main.db", &restored_path, RestoreOptions::default().version(versions[0].version))
        .await
        .unwrap();
    assert_eq!(restored.version, versions[0].version);
    assert_eq!(count_items(&restored_path), 10);
    let existing = web2.databases().restore("main.db", &restored_path, RestoreOptions::default()).await;
    assert_eq!(existing.expect_err("an existing file was overwritten").code(), ErrorCode::FILE_EXISTS);
    let head_path = server.path(&["restored", "head.db"]);
    web2.databases()
        .restore("main.db", &head_path, RestoreOptions::default().time(jiff::Timestamp::now()))
        .await
        .unwrap();
    assert_eq!(count_items(&head_path), 15);

    let replica_path = server.path(&["reader", "main.db"]);
    let replica = reader.replicas().start("main.db", ReplicaOptions::default().path(&replica_path)).await.unwrap();
    assert_eq!(replica.role, Role::REPLICA);
    assert_eq!(count_items(&replica_path), 15);
    assert_eq!(reader.replicas().list().await.unwrap().replicas.len(), 1);

    let tenant_path = server.path(&["web-1", "42.db"]);
    add_items(&tenant_path, 4);
    let acquired = web1
        .databases()
        .acquire("tenants/42.db", AcquireOptions::default().path(&tenant_path).maintenance(true))
        .await
        .unwrap();
    assert_eq!(acquired.decision.as_str(), "created");
    assert!(web1.databases().writer("tenants/42.db").await.unwrap().this_machine);
    add_items(&tenant_path, 2);
    let released =
        web1.databases().release("tenants/42.db", ReleaseOptions::default().durability(Durability::SERVER)).await;
    assert!(released.unwrap().database.synced_server.version >= 2);
    let moved_path = server.path(&["web-2", "42.db"]);
    let moved = web2.databases().acquire("tenants/42.db", AcquireOptions::default().path(&moved_path)).await.unwrap();
    assert_eq!(moved.decision.as_str(), "downloaded");
    assert_eq!(count_items(&moved_path), 6);
    add_items(&moved_path, 1);
    web2.databases().release("tenants/42.db", ReleaseOptions::default().durability(Durability::SERVER)).await.unwrap();

    // Writes the head never saw, made after web-2 moved the head on, diverge:
    // the copy is refused and left as it is, unless discard_local.
    add_items(&tenant_path, 3);
    let diverged = web1.databases().acquire("tenants/42.db", AcquireOptions::default().path(&tenant_path)).await;
    assert_eq!(diverged.expect_err("a diverged copy was acquired").code(), ErrorCode::CONFLICT);
    assert_eq!(count_items(&tenant_path), 9);
    let replaced = web1
        .databases()
        .acquire("tenants/42.db", AcquireOptions::default().path(&tenant_path).discard_local(true))
        .await
        .unwrap();
    assert_eq!(replaced.decision.as_str(), "replaced");
    assert_eq!(count_items(&tenant_path), 7);
    web1.databases().release("tenants/42.db", ReleaseOptions::default().durability(Durability::SERVER)).await.unwrap();
    server.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pool_lease_writes_rolls_back_what_it_did_not_commit_and_offloads_on_release() {
    let Some(mut server) = Server::start().await else {
        eprintln!("skipping: set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH");
        return;
    };
    let worker = server.connect("worker").await;
    let path = server.path(&["worker", "7.sqlite"]);
    let pool = worker.databases().pool(RusqliteAdapter::default(), PoolOptions::default()).unwrap();
    let options = LeaseOptions::default().path(&path).durability(Durability::CLOUD).offload(true);
    pool.lease("agents/7.sqlite", options, async |db| {
        db.call(|db| {
            db.execute_batch("CREATE TABLE items (id INTEGER PRIMARY KEY, body BLOB)")?;
            for _ in 0..3 {
                db.execute_batch("INSERT INTO items (body) VALUES ('rust')")?;
            }
            db.execute_batch("BEGIN; INSERT INTO items (body) VALUES ('never committed');")
        })
        .await
    })
    .await
    .unwrap();
    pool.close().await.unwrap();
    let status = worker.databases().get("agents/7.sqlite").await.unwrap();
    assert_eq!(status.state.as_str(), "offloaded");
    assert_eq!(status.synced_server.version, status.synced_cloud.version);
    let back = server.path(&["back", "7.sqlite"]);
    worker.databases().restore("agents/7.sqlite", &back, RestoreOptions::default()).await.unwrap();
    assert_eq!(count_items(&back), 3);
    server.stop().await;
}

/// The rusqlite adapter whose first close fails and hands the connection
/// back, as a driver that still holds the file would.
struct FirstCloseFails {
    adapter: RusqliteAdapter,
    failed: std::sync::atomic::AtomicBool,
}

impl Adapter for FirstCloseFails {
    type Connection = RusqliteConnection;

    fn open(&self, path: &Path) -> impl Future<Output = Result<RusqliteConnection, BoxError>> + Send {
        self.adapter.open(path)
    }

    async fn close(&self, connection: RusqliteConnection) -> Result<(), CloseError<RusqliteConnection>> {
        if !self.failed.swap(true, std::sync::atomic::Ordering::SeqCst) {
            return Err(CloseError::retry("a statement still holds the file", connection));
        }
        self.adapter.close(connection).await
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failed_close_keeps_the_database_acquired_until_release() {
    let Some(mut server) = Server::start().await else {
        eprintln!("skipping: set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH");
        return;
    };
    let worker = server.connect("close").await;
    let path = server.path(&["close", "9.sqlite"]);
    let adapter = FirstCloseFails { adapter: RusqliteAdapter::default(), failed: false.into() };
    let pool = worker.databases().pool(adapter, PoolOptions::default()).unwrap();
    let options = LeaseOptions::default().path(&path).durability(Durability::SERVER);
    pool.lease("agents/9.sqlite", options, async |db| {
        db.call(|db| db.execute_batch("CREATE TABLE items (id INTEGER PRIMARY KEY, body BLOB)")).await
    })
    .await
    .unwrap();
    let error = pool.close().await.expect_err("the pool closed");
    let cleanup = &error.errors[0];
    assert_eq!(cleanup.code(), ErrorCode::LEASE_CLEANUP_FAILED);
    assert_eq!(cleanup.lease_cleanup().map(|cleanup| cleanup.step.as_str()), Some("close"));
    let details = serde_json::json!({"step": "close", "name": "agents/9.sqlite", "path": path, "acquired": true});
    assert_eq!(serde_json::Value::Object(cleanup.details().clone()), details);
    assert!(worker.databases().writer("agents/9.sqlite").await.unwrap().this_machine);
    pool.release("agents/9.sqlite").await.unwrap();
    assert!(!worker.databases().writer("agents/9.sqlite").await.unwrap().this_machine);
    server.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn fails_over_and_denies_the_old_origins_diverged_file() {
    let Some(mut server) = Server::start().await else {
        eprintln!("skipping: set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH");
        return;
    };
    let first = server.connect("first").await;
    let second = server.connect("second").await;
    let first_path = server.path(&["first", "shared.db"]);
    add_items(&first_path, 5);
    let first_failover =
        first.failovers().start(&first_path, FailoverOptions::default().name("shared.db")).await.unwrap();
    assert!(first_failover.is_origin());
    first.databases().sync("shared.db").await.unwrap();
    let second_path = server.path(&["second", "shared.db"]);
    let second_failover =
        second.failovers().start(&second_path, FailoverOptions::default().name("shared.db")).await.unwrap();
    let mut second_notices = second_failover.notices().unwrap();
    assert_eq!(second_failover.role(), Role::REPLICA);
    eventually(Duration::from_secs(30), || if count_items(&second_path) == 5 { Ok(()) } else { Err("not replicated") })
        .await;

    server.kill("first");
    let killed = Instant::now();
    let promoted = tokio::time::timeout(Duration::from_secs(30), async {
        while let Some(notice) = second_notices.next().await {
            if matches!(&notice, FailoverNotice::Role(role) if *role == Role::ORIGIN) {
                return true;
            }
        }
        false
    })
    .await;
    assert_eq!(promoted, Ok(true), "the replica never took over");
    assert!(killed.elapsed() < Duration::from_secs(6), "took over after {:?}", killed.elapsed());
    assert!(second_failover.is_origin());
    // The old origin's follower lost its daemon, so its role is unknown.
    eventually(Duration::from_secs(5), || {
        if first_failover.role() == Role::UNKNOWN { Ok(()) } else { Err("still known") }
    })
    .await;
    // The old origin's app kept writing while its daemon was gone.
    add_items(&first_path, 3);

    let restarted = server.reconnect("first").await;
    let _ = restarted.failovers().start(&first_path, FailoverOptions::default().name("shared.db")).await;
    let denied_path = first_path.display().to_string();
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        let page = restarted.events().page(EventsOptions::default().limit(10_000)).await.unwrap();
        let denied = page.events.iter().any(|event| {
            event.r#type.as_str() == "denied"
                && event.name.as_deref() == Some("shared.db")
                && event.path.as_deref() == Some(denied_path.as_str())
        });
        if denied {
            break;
        }
        assert!(Instant::now() < deadline, "no denied event naming the file");
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    assert_eq!(count_items(&first_path), 8);
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert!(second_failover.is_origin());
    second_failover.stop().await.unwrap();
    server.stop().await;
}
