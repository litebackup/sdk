# How to back up SQLite while your app is running

Use a tool that reads a consistent SQLite database while your app keeps
writing. Litebackup handles continuous backups; SQLite's `.backup` command
creates a local snapshot when you need one file.

## Back up continuously with Litebackup

[Install your server](litebackup-server.md) and [join the app machine](litebackup-client.md),
then point Litebackup at the file:

```sh
litebackup backup /var/lib/app/main.sqlite
```

Or register it from your app:

```ts
import { backup } from "@litebackup/sdk";

backup("/var/lib/app/main.sqlite");
```

Your app keeps using its SQLite driver. Changes sync in the background;
there's no need to pause writes or wait for an upload during startup.
See [SQLite backup setup](sqlite-backup.md) for naming, status, and error handling.

## Create a local snapshot with sqlite3

The SQLite shell's `.backup` command uses SQLite's online backup API. Run it
against the database file, including when the app uses WAL mode:

```sh
mkdir -p /var/lib/app/exports
sqlite3 -bail /var/lib/app/main.sqlite <<'SQL'
.timeout 5000
.backup /var/lib/app/exports/main.sqlite
SQL
sqlite3 -readonly /var/lib/app/exports/main.sqlite 'PRAGMA quick_check'
```

Use a new export filename for each snapshot you want to keep. `quick_check`
should print `ok`. The copy reflects a consistent database state; your app
can keep writing to the source afterwards. SQLite documents this in its
[online backup API guide](https://www.sqlite.org/backup.html).

To send the export to your Litebackup server and bucket, use the
[backup script](sqlite-backup-script.md) or [Cron job](sqlite-cron-backup.md).
Keep exports outside directories covered by your app's continuous backup.

## What about the SQLite WAL file?

In WAL mode, recent commits can be in `main.sqlite-wal` before a checkpoint
moves them into `main.sqlite`. Use Litebackup or SQLite's backup API to read
the database as a whole, rather than copying the main file by itself while
the app is running. You don't need to force a checkpoint before each backup.

Keep the database and WAL files together on a persistent disk or volume.
Leave the journal files in place while connections are open; SQLite manages
them. See [SQLite's WAL documentation](https://www.sqlite.org/wal.html) and
[SQLite in Docker](sqlite-docker.md) for storage details.

## Check that you can restore

Restore into a new file, run an integrity check, and query a few records your
app depends on. See [SQLite point-in-time restore](sqlite-restore.md) for the
CLI and SDK examples, or [recover deleted rows](sqlite-deleted-rows.md) when
you only need a few records back.
