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

## Start continuous backup

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

await litebackup.backups.start("/var/lib/app/main.sqlite");
```

```sh
litebackup start ./main.sqlite
litebackup start '/data/**/*.sqlite' tenants/
litebackup status --limit 100
```

Registration saves a local rule. The daemon lists the rule's current and future files and uploads changes. Check each file's status for writer conflicts and sync errors. The first status page also shows the paths a rule cannot read (`unreadable_paths`), including invalid filenames. Pass the last name on a page to `status --after NAME` for the next page.

## Name your databases

A file defaults to its filename, with case and spelling preserved. A second argument sets an exact name or a prefix ending in `/`. Directory and glob rules take prefixes. The recursive example includes files directly in `/data` and maps `/data/a/b/42.sqlite` to `tenants/a/b/42.sqlite`. You can supply a symlink to a file or directory, including the directory before a glob. A continuous rule keeps the target resolved at registration. A file link uses the link’s filename as its default remote name. Listing the rule's files skips symlinks it meets inside the tree and SQLite `-wal`, `-shm`, and `-journal` files. Quote globs to let Litebackup expand them.

Status shows the paths a rule cannot read. A persistent failure produces one `path_unreadable` event per rule and path. A changed error or a failure that recurs after recovery produces a new event.

Names accept Unicode letters, marks and numbers, plus spaces, dots, underscores and hyphens. Separate segments with `/`. Empty segments, `.` and `..` are invalid.

## Stop or inspect

```sh
litebackup rules
litebackup stop main.sqlite
litebackup start ./main.sqlite tenants/
litebackup stop tenants/main.sqlite
```

Use the exact remote database name even if the source file moved or was deleted. For a directory or glob, copy the ID from `litebackup start` or `litebackup rules`, then run `litebackup stop --rule ID`. This removes the entire backup rule and ends its future listings. Trying to stop one of its databases by name shows its owning rule and leaves the group running.

Stopping releases locks and forgets the mapping. Local files stay on disk and remote versions follow retention. If a release is slow or the server is offline, stop reports a pending lease and the daemon retries in the background. Other databases continue renewing their leases. Stop performs no final upload.

## Wait for a backup

```sh
litebackup backup ./export.sqlite tenants/42.sqlite
litebackup restore tenants/42.sqlite "${PWD}/restored.sqlite"
```

Foreground jobs wait for server acknowledgement by default. Cloud waits include the archive delay. Each name has one active writer. After release, another authorized writer can publish its local database as the next version, including different contents. Litebackup does no row merge.
