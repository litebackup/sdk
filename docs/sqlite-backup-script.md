# Back up SQLite in a scheduled job

Run the foreground command on the host that holds the SQLite files, using the same credentials as the scheduled task.

```sh
litebackup backup '/data/exports/**/*.sqlite' scheduled/ --wait cloud --timeout 30m
```

Each matched file takes its writer lock, captures committed SQLite pages and waits for acknowledgement. A failed file produces an error and the job continues with other matches. A nonzero exit means the job failed. An overlapping writer gets `busy`. Files already published remain available to restore.

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

await litebackup.backups.once("/data/exports/**/*.sqlite", {
  prefix: "scheduled/",
  wait: "cloud",
  timeout: "30m",
});
```

A job exits nonzero when no files match or any file fails. The foreground process exits after cleanup. Use `litebackup start` for applications that need ongoing discovery and uploads.

[Runnable TypeScript example](../typescript/examples/one-shot.ts) · [Cron](sqlite-cron-backup.md) · [Temporal](sqlite-temporal.md)
