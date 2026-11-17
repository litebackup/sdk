# Schedule SQLite backups with Temporal

Put Litebackup calls in an **Activity**, where filesystem and network I/O are
allowed. Keep the Workflow deterministic. Run the Activity Worker on the
machine that holds the SQLite files, or on a machine with the same mounted
files and client access. A remote Temporal service does not need those files.

Install your [Litebackup server](litebackup-server.md), [join the database host](litebackup-client.md),
and use the [one-shot job rules](sqlite-backup-script.md) for file selection and naming.
This example backs up export files under `scheduled/` and uses a
self-hosted Temporal development server on `localhost:7233`; supply your
normal connection and TLS options for a production Temporal deployment.

## Install

In an ESM TypeScript app:

```sh
npm install @litebackup/sdk @temporalio/activity @temporalio/client @temporalio/worker @temporalio/workflow
npm install --save-dev typescript @types/node
```

These files use strict TypeScript, NodeNext modules, and ES2023. See the
[API release note](sdk-reference.md#release-compatibility) for cancellation support in
older SDKs.

## Activity: back up the files on this host

Save as `activities.ts`:

```ts
import { Context } from "@temporalio/activity";
import { backup } from "@litebackup/sdk";

export async function backupDatabases(paths: string[]): Promise<void> {
  if (paths.length === 0) throw new Error("Provide at least one database path");
  const context = Context.current();
  for (const path of paths) {
    const heartbeat = setInterval(() => context.heartbeat({ path }), 10_000);
    try {
      const run = backup(path, { prefix: "scheduled/" });
      await run.ready;
      try {
        await run.wait("cloud", {
          signal: AbortSignal.any([
            context.cancellationSignal,
            AbortSignal.timeout(30 * 60_000),
          ]),
        });
      } finally {
        await run.stop();
      }
    } finally {
      clearInterval(heartbeat);
    }
  }
}
```

Heartbeats let Temporal detect an unavailable Worker and deliver cancellation.
The Activity succeeds after all files reach the bucket and their backup rules
stop. A retry can back up already completed files again, possibly with newer
writes.

## Workflow: call the Activity

Save as `workflows.ts`:

```ts
import { proxyActivities } from "@temporalio/workflow";
import type * as activities from "./activities.js";

const { backupDatabases } = proxyActivities<typeof activities>({
  startToCloseTimeout: "90 minutes",
  heartbeatTimeout: "1 minute",
  retry: {
    initialInterval: "30 seconds",
    maximumInterval: "5 minutes",
    maximumAttempts: 3,
  },
});

export async function sqliteBackup(paths: string[]): Promise<void> {
  await backupDatabases(paths);
}
```

The type-only Activity import does not execute Litebackup inside the Workflow
sandbox. Size the Activity timeout for the whole list: two 30-minute waits
plus registration and cleanup fit this example's 90-minute limit.

## Worker: bind a queue to the database host

Save as `worker.ts`:

```ts
import { fileURLToPath } from "node:url";
import { NativeConnection, Worker } from "@temporalio/worker";
import * as activities from "./activities.js";

const connection = await NativeConnection.connect({ address: "localhost:7233" });
try {
  const worker = await Worker.create({
    connection,
    taskQueue: "sqlite-backup-app-1",
    workflowsPath: fileURLToPath(new URL("./workflows.js", import.meta.url)),
    activities,
    maxConcurrentActivityTaskExecutions: 1,
  });
  await worker.run();
} finally {
  await connection.close();
}
```

Compile to JavaScript before starting `worker.js`. Set `LITEBACKUP_TOKEN` for
first use, or run under the already joined client's OS user. This host-specific
queue should have one active Activity Worker for these local files. Do not
put unrelated database hosts on the same queue. The Workflow's path must
resolve to the compiled `workflows.js` beside the compiled worker.

## Create an hourly Schedule

Save as `schedule.ts` and run once after starting the Worker:

```ts
import { Client, Connection, ScheduleOverlapPolicy } from "@temporalio/client";

const connection = await Connection.connect({ address: "localhost:7233" });
try {
  const client = new Client({ connection });
  await client.schedule.create({
    scheduleId: "sqlite-backup-app-1-hourly",
    spec: { intervals: [{ every: "1 hour" }] },
    action: {
      type: "startWorkflow",
      workflowType: "sqliteBackup",
      taskQueue: "sqlite-backup-app-1",
      args: [["/var/lib/app/exports/main.sqlite", "/var/lib/app/exports/audit.sqlite"]],
    },
    policies: { overlap: ScheduleOverlapPolicy.SKIP },
  });
} finally {
  await connection.close();
}
```

A stable Schedule ID prevents creating duplicates accidentally; update the
existing Schedule instead of re-running create. `SKIP` avoids scheduled
Workflow overlap. It does not serialize separate manual Workflows, external
cron jobs, or timed-out Activity attempts that are still running. Keep one
owner for these files and names, cooperate with cancellation, and coordinate
retries before adding more Workers.

Alert on failed Workflows, prolonged retries, and missing recent successful
Schedules. Test cancellation and Worker restarts against your actual
filesystem arrangement before relying on scheduled persistence.

See Temporal's [TypeScript application guide](https://docs.temporal.io/develop/typescript/core-application)
and [Schedules guide](https://docs.temporal.io/develop/typescript/schedules)
for deployment and connection options. For a simpler scheduler on one host,
use [Cron](sqlite-cron-backup.md); the underlying [one-shot operation](sqlite-backup-script.md) is the same.
