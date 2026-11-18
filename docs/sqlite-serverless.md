# Back up SQLite from a disposable worker

Give the worker access to the SQLite file and its WAL, writable temporary
storage, network access to the server, and the value of `LITEBACKUP_TOKEN`
from **Machines → Add machine**. Select **Multiple machines** for recurring
jobs and allow enough uses for each call and any retries that enroll.
Await the upload before the invocation returns.

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
export async function run(): Promise<void> {
  await litebackup.backups.once("/data/main.sqlite", { wait: "cloud", timeout: "10m" });
}
```

Each fresh invocation joins with a temporary identity scoped by the token.
A running job renews its 15-minute credential. Completion or cancellation
releases the writer lease and retires the identity. After forced termination,
lease and credential expiry allow another job to proceed. Active jobs count
toward the machine limit until retirement or expiry. Each job consumes one token use, including jobs interrupted by a crash. Jobs writing the same name concurrently receive
`busy`. A worker with an existing joined session can reuse its identity.

Continuous backup needs a process and local storage that survive between
invocations. See [Docker](sqlite-docker.md) for a persistent application.
