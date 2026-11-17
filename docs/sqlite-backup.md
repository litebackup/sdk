# How to back up SQLite databases with Litebackup

Install a [server](litebackup-server.md), [connect the app machine](litebackup-client.md), and keep
using your current SQLite driver. Backups work with SQLite files in WAL or
rollback-journal mode. In-memory databases have no file to back up.

## Keep the files on persistent storage

SQLite handles local commits as usual while Litebackup syncs changes in the
background. Keep the database and its WAL files on a persistent disk or volume
that survives restarts and redeploys. With those files and its saved state
available, the client can resume syncing after a restart.

Use a persistent volume in containers. Memory-backed filesystems such as
tmpfs and container layers deleted on replacement don't keep a local copy
across deployments. See [container setup](litebackup-client.md#containers-and-short-lived-jobs)
for mounts and client state, or use the [Docker example](sqlite-docker.md).

## Start in the background

```ts
import { backup } from "@litebackup/sdk";

const main = backup("/var/lib/app/main.sqlite");
void main.ready.catch((error) => console.error("Backup registration failed", error));
```

A `better-sqlite3` handle is also accepted: `backup(db)` reads its filename.
The SDK does not import or bundle a SQLite driver.

Go waits for local registration, not initial server or cloud durability:

```go
run, err := litebackup.Backup(ctx, "/var/lib/app/main.sqlite", litebackup.BackupOptions{})
if err != nil {
    return err
}
_ = run // keep it if you need to wait or stop later
```

CLI registration also returns without an initial durability wait:

```sh
litebackup backup /var/lib/app/main.sqlite
```

The daemon keeps backing up after these calls return. You can call `backup(db)`
without keeping the handle. If it fails to start, Node reports an unhandled
rejection, which normally exits the process. Add a `ready.catch(...)` handler
if your app should handle that failure itself; `void` is optional. `ready`
only reports registration errors. Monitor [status and errors](sqlite-backup-errors.md)
for failures after the backup starts.

## Name your databases

The default name is the basename, lowercased, with characters outside
`[a-z0-9._-]` replaced by `-`. `My App.sqlite` becomes `my-app.sqlite`.
`prefix: "app/"` produces `app/my-app.sqlite`; `name` sets an exact name.
Relative paths resolve against the app's working directory.

```ts
const databases = backup("/var/lib/app/*.sqlite", { prefix: "app/" });
void databases.ready.catch(console.error);
```

```sh
litebackup backup '/var/lib/app/*.sqlite' --prefix app/
```

Quote the CLI glob so Litebackup, rather than the shell, watches matching
files, including new ones. Globs apply to the final path segment; they are not
recursive directory scans. Use one rule per directory. Do not use `name`
with a multi-file glob.

A remote name has one origin at a time. `name_in_use` protects an unrelated
file from replacing an existing database. Change the prefix only when the
files represent separate logical databases. Use acquire/release to move the
same database between machines.

## Wait for a backup

| Operation | What completion means |
| --- | --- |
| `backup(path)` | A TypeScript handle exists; registration is still in progress |
| `await run.ready` | The local daemon registered the backup, unless an explicit `wait` option was supplied |
| Go `Backup(...)` | Registration completed, unless an explicit `Wait` option was supplied |
| `await run.wait("server")` | Changes written before this call reached your Litebackup server |
| `await run.wait("cloud")` | That synced version also reached your encrypted bucket |
| CLI `backup PATH --wait cloud --timeout 30m` | Initial backup reached the bucket within the configured wait |

Keep these waits out of app startup. Use them before finishing a script or
handing a database to another machine. They cover changes already written;
future commits stay in the local SQLite files and sync in the background.

Cloud completion can include the server's configured archive delay. Pick a
job deadline that allows for that delay and a full initial upload. A zero-byte
new database can stay at version zero until the first actual write.

For a local snapshot of a running database, see [SQLite live backups and WAL](sqlite-live-backup.md).

## Stop or inspect

```ts
import { sharedClient } from "@litebackup/sdk";

const client = await sharedClient();
console.log(await client.database("main.sqlite"));
console.log(await client.backups.list());
```

Stopping does not erase backups or delete the local file. For a finite job,
wait before stopping; see [one-shot examples](sqlite-backup-script.md). For databases
that move between machines, see [agents](sqlite-agent-state.md).
