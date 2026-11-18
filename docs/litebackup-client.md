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
curl -fsSL https://litebackup.com/cli.sh | sudo bash
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
litebackup up "$LITEBACKUP_TOKEN"
litebackup backup '/var/lib/app/*.sqlite' --prefix app/
```

`LITEBACKUP_TOKEN` and the SDK's `token` option accept a join token and server
address: `JOIN_TOKEN@HOST`. They do not accept a server API key. On first join,
the server gives the client its own machine API key, which it saves locally
and uses for later requests. You can omit the join token after that as long
as the client's saved state remains available.

The SDK can join on first use, so `up` is optional for SDK apps. A client that
already joined keeps its existing server and identity; changing the token
does not move that session to another server. Server API keys are for direct
requests to the server's HTTPS API; this SDK talks to the local client.

For the service install, use an existing OS user that can read your app files:

```sh
sudo litebackup install service --user app --join "$LITEBACKUP_TOKEN"
sudo litebackup backup '/var/lib/app/*.sqlite' --prefix app/
sudo systemctl status litebackup
sudo journalctl -u litebackup -f
```

Root and the configured service user reach the service socket. A user who
ran `litebackup install service` without sudo reaches their own service from
any environment, cron included: the install records its socket directory in
`~/.config/litebackup/service.json`. Another user can connect to a different
session instead, so run cron, app, and CLI under consistent ownership. File permissions apply to the daemon's user. Restore,
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
`Litebackup.connect({ token, session })` for an explicit client; Go uses
`litebackup.Connect(ctx, litebackup.Options{Token: token, Session: session})`.
Package helpers share a client per token and session.

Session names use `[a-z0-9._-]` and start with a letter or digit. SDKs start
`litebackup launch --session NAME` when the socket is absent. A launch lock prevents concurrent processes from
starting duplicate clients for one session.

Sessions isolate local configuration, not remote database names. Two sessions
backing up `main.sqlite` can still collide on the server. Prefix names by
application or shard when they represent different databases.

## App exit and reboot

`close()` / `Close()` closes this SDK connection and stops its event watchers.
The daemon keeps running and syncing after your app exits. Use `wait()` when
a script needs to confirm its changes have reached the server or bucket.

`backup.stop()` / `BackupRun.Stop()` stops that registered backup and releases
its write locks. It keeps local files and remote backups. It does not perform
a final durability wait; use `wait()` first for a [one-shot job](sqlite-backup-script.md).

`release()` is for databases obtained with `acquire()`. Close every SQLite
connection before releasing. Use the background backup's `stop()` for backups.

`shutdown()` / `Shutdown()` drains and stops the entire session. Only use it
when your process owns that session; another app can be sharing it. A service
install handles daemon restarts and reboot startup through systemd.

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
