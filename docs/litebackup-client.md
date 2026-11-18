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
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
litebackup up "$LITEBACKUP_TOKEN"
litebackup start '/var/lib/app/*.sqlite' app/
```

`LITEBACKUP_TOKEN` and the SDK's `token` option accept a join token and server
address: `JOIN_TOKEN@HOST`. They do not accept a server API key. On first join,
the server gives the client its own machine API key, which it saves locally
and uses for later requests. You can omit the join token after that as long
as the client's saved state remains available.

The SDK can join on first use, so `up` is optional for SDK apps. A client that
already joined keeps its existing server and identity; changing the token
does not move that session to another server. Server API keys are for direct
requests to the [HTTPS API](http-api.md); this SDK talks to the local client.

To keep backing up across reboots, install the client as a service. Run it as
the user that owns your app's databases:

```sh
litebackup install service --join "$LITEBACKUP_TOKEN"
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
sudo litebackup install service --user app --join "$LITEBACKUP_TOKEN"
sudo systemctl status litebackup
```

Root and the system service's user reach the system service's socket. Another
user connects to a different session instead, so run cron, app, and CLI under
consistent ownership. File permissions apply to the daemon's user. Restore,
replica, acquire, and offload also need directory write permissions.

## Sessions and configuration

| Setting | Meaning |
| --- | --- |
| `LITEBACKUP_TOKEN` | `JOIN_TOKEN@HOST` used when the client first joins |
| `LITEBACKUP_SESSION` | Session name; `default` when unset |
| `LITEBACKUP_PATH` | Override the client binary path |
| `XDG_RUNTIME_DIR` | Root for per-user sockets when no service install recorded one |
| `XDG_STATE_HOME` | Root for the client's persistent per-user state |

A TypeScript `token` or `session` option overrides the environment. Use
`new Litebackup({ token, session })` for an explicit client; Go uses
`litebackup.Connect(ctx, &litebackup.ConnectArgs{Token: token, Session: session})`.
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
when your process owns that session; another app can be sharing it. A service
install handles daemon restarts and reboot startup through systemd or launchd.

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
