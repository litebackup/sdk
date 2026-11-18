# Litebackup for TypeScript

Back up SQLite to your Litebackup server and bucket. Install the SDK with
`npm install @litebackup/sdk` on Linux or macOS with Node.js 22 or later.
In **Machines → Add machine**, copy the value for `LITEBACKUP_TOKEN` and set
it in your application's environment.

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
const rule = await litebackup.backups.start("./main.sqlite");
console.log(rule.id);
```

The rule is saved locally before discovery and uploads finish. Keep the
daemon and its local state on persistent storage. For a scheduled job, await
a foreground backup and choose the acknowledgement you need.

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.backups.once("./export.sqlite", { wait: "cloud" });
```

A fresh worker spends one token use on each call to `backups.once()`, including
retry attempts that enroll. For a recurring job, select **Multiple machines**
in **Machines → Add machine** and set the use limit and expiry for the schedule.
A joined session reuses its saved identity. See the [SDK reference](https://github.com/litebackup/sdk/blob/main/docs/sdk-reference.md)
and [migration notes](https://github.com/litebackup/sdk/blob/main/CHANGELOG.md).
