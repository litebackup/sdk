# Back up multiple SQLite databases in a script

Use this in scripts and scheduled tasks that need to wait for a backup before
exiting. For a running app, use [continuous backup](sqlite-backup.md).

The job must run on a machine with the SQLite files and a [joined client](litebackup-client.md).
Use files not already covered by a continuous backup, such as SQLite exports
in `/var/lib/app/exports`. Serialize overlapping runs and reserve `scheduled/`
for this job. Registering a backed-up file under another name fails; reusing
its existing name and calling `stop()` would stop the app's backup.

## A file already backed up by your app

Use its existing handle to wait for the bucket, then leave the backup running:

```ts
await main.wait("cloud", { signal: AbortSignal.timeout(30 * 60_000) });
```

With the CLI, repeat the original registration with a wait and no stop:

```sh
litebackup backup /var/lib/app/main.sqlite --wait cloud --timeout 30m
```

Keep the original name and prefix options if you used them. For separate
export files, use the start/wait/stop examples below.

Create exports with [SQLite's online backup command](sqlite-live-backup.md#create-a-local-snapshot-with-sqlite3).

## One export file

```ts
import { backup } from "@litebackup/sdk";

const run = backup("/var/lib/app/exports/main.sqlite", { prefix: "scheduled/" });
await run.ready;
try {
  await run.wait("cloud", { signal: AbortSignal.timeout(30 * 60_000) });
} finally {
  await run.stop();
}
```

`wait("cloud")` syncs to the server, then waits for the bucket. The signal
cancels that wait, including in-flight requests, after registration completes.
For a limit on the whole process, set a deadline in your job runner. See the
[API release note](sdk-reference.md#release-compatibility) if using an older SDK.

The 30-minute limit allows for the default 15-minute archive delay plus upload
time. Increase it for a longer policy delay or a large first backup.

`stop()` releases the backup's write locks and keeps local files and remote
backups. Calling it in `finally` also stops the rule when the wait fails. Fail
the job and keep the local files if either operation fails. Cancellation does
not undo a sync already accepted by the server.

## A list, one at a time

```ts
import { backup } from "@litebackup/sdk";

export async function backupFiles(paths: string[]): Promise<void> {
  if (paths.length === 0) throw new Error("Provide at least one SQLite path");
  for (const path of paths) {
    const run = backup(path, { prefix: "scheduled/" });
    await run.ready;
    try {
      await run.wait("cloud", { signal: AbortSignal.timeout(30 * 60_000) });
    } finally {
      await run.stop();
    }
    console.log(`Backed up ${run.name} to cloud`);
  }
}
```

The loop backs up one file at a time and stops at the first error. Keep names
stable between runs. Two different files named `main.sqlite` need different
names or prefixes. Each file is synced separately, so the list is not one
atomic snapshot. Writes made after a file's sync may be outside this backup.

The [runnable implementation](../typescript/examples/backup-files.ts) and
[command entry point](../typescript/examples/one-shot.ts) build with this SDK:

```sh
cd typescript
npm ci --omit=optional
npm run build
# Supply an installed binary because optional packages were omitted here:
export LITEBACKUP_PATH=/absolute/path/to/litebackup
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
node dist/examples/one-shot.js /var/lib/app/exports/main.sqlite /var/lib/app/exports/audit.sqlite
```

In your own app, import from `@litebackup/sdk`. The runnable example imports
this repository's source.

## CLI alternative

For a fixed file on a joined machine:

```sh
litebackup backup /var/lib/app/exports/main.sqlite --prefix scheduled/ --wait cloud --timeout 30m
litebackup backup stop /var/lib/app/exports/main.sqlite
```

If the first command times out, the rule may still exist. A production shell
job should use a cleanup trap, check exit codes, and serialize executions;
the [Cron guide](sqlite-cron-backup.md) includes a complete script.

See [client lifecycle](litebackup-client.md#app-exit-and-reboot) for the differences between
`stop`, `release`, and `shutdown`.

See [Go](sqlite-go.md#one-shot-backup) for waits with a context deadline,
[Cron](sqlite-cron-backup.md) for host and Kubernetes scheduling, and [Temporal](sqlite-temporal.md)
for an activity that runs this list on the database host.
