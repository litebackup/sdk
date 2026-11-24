# How to run the Litebackup client

The CLI and SDKs talk to a local Litebackup client over a Unix socket. That
client reads SQLite files, sends changes to your [server](litebackup-server.md), and keeps
syncing independently of the app process.

## Choose how to run it

| Environment | Setup |
| --- | --- |
| Node.js app | `npm install @litebackup/sdk`; it includes the platform binary and starts the client on demand |
| Go app | Install the `litebackup` CLI and `go get github.com/litebackup/sdk/litebackup`; the SDK starts it on demand |
| Existing app, no SDK | Install the CLI, join, then register a path or glob |
| Linux host that must resume after reboot | Install the client as a systemd service under the app's OS user |
| Container | Run the app and client with access to the same database files and persistent client state |

Supported client binaries: Linux and macOS, amd64/x64 and arm64. Unix sockets
are required; Windows is not a supported client target.

## Join and register

With a join token from **Machines → Add machine**:

```sh
curl -fsSL https://litebackup.com/cli.sh | bash
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
litebackup up "$LITEBACKUP_API_KEY"
litebackup start '/var/lib/app/*.sqlite' app/
```

`LITEBACKUP_API_KEY`, the SDK's API key option, and `--api-key` take one
credential, then @ and the server address: a join token (`JOIN_TOKEN@HOST`) or
an API key (`API_KEY@HOST`). The client tells them apart by prefix: a join token
starts with `litebackup_join_`, an API key with `litebackup_`. It exchanges a
join token once for the machine's own API key; it uses an API key as it is.
Either is saved locally and used for later requests, so you can omit the
variable as long as the client's saved state remains available. An invite
token, or any other value, is refused with `invalid_argument`.

The SDK can join on first use, so `up` is optional for SDK apps. When the
client starts, it uses the key it saved unless the variable holds a different
credential: the same join token, or the same API key, for the same server
address keeps the saved key and joins nothing. A different one replaces the
saved key, joining again for a join token, and can move the session to
another server. A client that is already running keeps its key until it
restarts. `litebackup up` follows the same rule: run again with the
credential the session joined with, it keeps the machine and joins nothing.
With another credential, `up` saves the new key and restarts a running
client in place.

A write lock belongs to the key it was taken with. While the client holds
any, a different credential is refused with `drain_required`, and nothing
is sent or saved: from `up`, from a client start with another
`LITEBACKUP_API_KEY`, and from an SDK that passes another `apiKey`. To
change the key, drain first, then use the new credential:

```sh
litebackup drain
litebackup up "$NEW_API_KEY"
```

An SDK app calls its shutdown (`Shutdown`, `session.shutdown()`) with the
current credential, then connects with the new one. With no write lock held,
the new credential is used at once. A client that stopped without a drain
is started with its stored credential by `litebackup drain`, which then
drains it. With a service, remove it, which drains it first and keeps
this machine's join, then install it again with the new credential, as the
same user and with the same session and state directory:

```sh
litebackup uninstall service
litebackup install service --api-key "$NEW_API_KEY"
```

When the old key does not work, as once it is revoked, no drain can
release its locks. `litebackup up --drop-locks "$NEW_API_KEY"` saves the new
key and drops them at the client's next start; each database takes a new
lock at its next sync, once the old session's lease ran out, and is checked
against the server's head as after any drain.

To keep backing up across reboots, install the client as a service. Run it as
the user that owns your app's databases:

```sh
litebackup install service --api-key "$LITEBACKUP_API_KEY"
litebackup start '/var/lib/app/*.sqlite' app/
systemctl --user status litebackup
journalctl --user -u litebackup -f
```

On Linux this is a systemd user service, which starts at boot when lingering
can be turned on and at login otherwise. On macOS it is a launchd agent, which
starts at login. The install records the service's socket directory in
`~/.config/litebackup/service.json`, so that user's CLI, SDKs, and cron jobs
reach it from any environment.

Run it as root for a system service that reaches every database, with
`--user app` to run it as your app's user:

```sh
sudo litebackup install service --user app --api-key "$LITEBACKUP_API_KEY"
sudo systemctl status litebackup
```

Root and the system service's user reach the system service's socket. Another
user connects to a different session instead, so run cron, app, and CLI under
consistent ownership. File permissions apply to the daemon's user. Restore,
replica, acquire, and offload also need directory write permissions.

## Sessions and configuration

| Setting | Meaning |
| --- | --- |
| `LITEBACKUP_API_KEY` | `JOIN_TOKEN@HOST` or `API_KEY@HOST`, used when the client starts |
| `LITEBACKUP_SESSION` | Session name; `default` when unset |
| `LITEBACKUP_PATH` | Override the client binary path |
| `XDG_RUNTIME_DIR` | Root for per-user sockets when no service install recorded one |
| `XDG_STATE_HOME` | Root for the client's persistent per-user state |

A TypeScript `apiKey` or `session` option overrides the environment. Use
`new Litebackup({ apiKey, session })` for an explicit client; Go uses
`litebackup.Connect(ctx, &litebackup.ConnectArgs{APIKey: apiKey, Session: session})`.
Each TypeScript client connects on its first socket operation. Go's socket
helpers use a single process-wide client.

Session names use `[a-z0-9._-]` and start with a letter or digit. SDKs start
`litebackup launch --session NAME` when the socket is absent. A launch lock prevents concurrent processes from
starting duplicate clients for one session.

Sessions isolate local configuration, not remote database names. Two sessions
backing up `main.sqlite` can still collide on the server. Prefix names by
application or shard when they represent different databases.

## App exit and reboot

`close()` / `Close()` closes this SDK connection and stops its event watchers. TypeScript also cancels foreground jobs started by this client.
The daemon keeps running and syncing after your app exits.

Use `backups.stop(name)` or Go `StopBackup(ctx, name)` for a single database by remote name. Use `rules.stop(id)` or `StopBackupRule(ctx, id)` to remove a whole backup rule. The files and retained versions stay available. A scheduled job can call `backups.once()` with a server or cloud wait.

Use `release()` for databases obtained with `acquire()`. Close every SQLite connection before releasing.

`session.shutdown()` / `Shutdown()` drains and stops the entire session. Only use it
when your process owns that session; another app can be sharing it. A backup
another machine writes holds no lock to release, so the drain leaves it
waiting, and the next start waits for the lock again. A database the drain
cannot sync or release is listed in `failed` with its code and message, and
keeps any write lock it holds, and so is a conflicted copy whose writes
are not on the server, with `conflict` and a message naming its file; then
the session and the daemon stay, so
check `failed`: it is empty only when every database was synced as asked
and released.
A service install handles daemon restarts and reboot startup through systemd or launchd.

## Containers and short-lived jobs

For a working app with persistent volumes, see [SQLite backup in Docker](sqlite-docker.md).

The client must see the database file, its `-wal` and `-shm` files, and the
same paths your SDK sends. Keep them together on a persistent volume that
survives container replacement. Persist the client's state too, so it keeps
its machine identity and can resume syncing. For a per-user client, set and
mount `XDG_STATE_HOME`; keep `XDG_RUNTIME_DIR` writable for its socket.

The npm package brings the executable through optional dependencies. Avoid
`--omit=optional` in a deployed app unless you separately supply
`LITEBACKUP_PATH`. A Go image needs an installed compatible binary, not just
the Go module.

A remote CI runner or Temporal worker cannot back up another machine's path
through the SDK. Run the job on a machine with access to the files. A
short-lived job using temporary storage should [wait for the bucket](sqlite-backup-script.md)
before removing that storage. Kubernetes volume access modes also constrain where a
[CronJob](sqlite-cron-backup.md#kubernetes-cronjob) can run.
