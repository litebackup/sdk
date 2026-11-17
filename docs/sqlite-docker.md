# How to back up SQLite in Docker with Litebackup

Keep your SQLite files on a persistent volume, and run the Litebackup client
in the same container as your app. The app writes locally while the client
syncs in the background. The volume keeps those files available across
container replacements.

## Run the example app

[Install a Litebackup server](litebackup-server.md) and copy a join token from
**Machines → Add machine**. From this repository's root:

```sh
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
docker compose -f examples/docker/compose.yaml up --build -d
curl -X POST http://localhost:3000
curl http://localhost:3000
```

The example stores a visit count in `/data/main.sqlite`. Each POST adds a row;
a GET returns the count. It binds the host port to localhost. You can set
`APP_PORT` if port 3000 is already in use.

The [app](../examples/docker/app.mjs) uses Node's SQLite driver and registers
the file with `backup()`. Its [Dockerfile](../examples/docker/Dockerfile)
installs the SDK with its optional binary package and runs as `node` (UID 1000).
[Compose](../examples/docker/compose.yaml) starts the container again after a
host reboot. The join token is only needed for the client's first join.

## Persist the database and client state

| Path in the example | Volume | Purpose |
| --- | --- | --- |
| `/data` | `databases` | The database and its SQLite journal files |
| `/state` | `client-state` | Client identity and saved backup rules |
| `/tmp/litebackup-runtime` | Container filesystem | Temporary socket directory; recreated on startup |

Mount the whole database directory so `main.sqlite` and its `-wal` and `-shm`
files stay together. Keep client state persistent too, so a replacement
container reconnects as the same machine.

For an existing app, use the same mounts and set `XDG_STATE_HOME=/state`.
Match the container UID and volume permissions to your app. The client needs
access to the same database paths your SDK passes to it. Only one client
should use a particular state volume at a time.

See the [client guide](litebackup-client.md#containers-and-short-lived-jobs)
for runtime details. The SDK includes Linux x64 and arm64 binaries; retain
npm's optional dependencies in the image.

## Replace the container and keep the data

```sh
docker compose -f examples/docker/compose.yaml up -d --force-recreate
curl http://localhost:3000
```

The count remains because Compose reattaches the named volumes. Ordinary
`docker compose down` also keeps these volumes. Use `down -v` only when you
intend to delete the example's database and client state.

Keep normal app startup free of upload waits. A [short-lived backup job](sqlite-backup-script.md)
using temporary files should wait for the bucket before removing those files.
For a Kubernetes schedule, use the [CronJob example](sqlite-cron-backup.md#kubernetes-cronjob).

## Check the remote backup

Open the Litebackup console to see `main.sqlite` and its server and cloud
versions. Use the [restore guide](sqlite-restore.md) to restore it into a new
file on another joined machine and inspect the data.
