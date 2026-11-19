# Litebackup for TypeScript

Back up SQLite to your Litebackup server and bucket. Install the SDK with
`npm install @litebackup/sdk` on Linux or macOS with Node.js 22 or later.
In **Machines → Add machine**, copy the value for `LITEBACKUP_TOKEN` and set
it in your application's environment.

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";
import { databasePath } from "@litebackup/sdk/better-sqlite3";

const db = new Database("data/main.sqlite");
const litebackup = new Litebackup();
const rule = await litebackup.backups.start(databasePath(db));
console.log(rule.id);
```

The rule is saved locally before discovery and uploads finish. Call
`backups.start` on every start: the same path and settings return the saved
rule. Keep the daemon and its local state on persistent storage.

Each driver has its own subpath export, so the SDK never guesses what a
handle is:

| Import | Path of a handle | Managed-database adapter |
| --- | --- | --- |
| `@litebackup/sdk/better-sqlite3` | `databasePath(db, { schema? })` | `adapter(Database, options?)` |
| `@litebackup/sdk/node-sqlite` | `databasePath(db, { schema? })` | `adapter(options?)` |
| `@litebackup/sdk/drizzle` | `databasePath(drizzleDb)` | the client's adapter |

With any other driver, pass the filename or a `file:` URL.

For a worker that moves a database between machines, a managed scope acquires
it, opens it, runs your code, closes it, and releases it:

```ts
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
await litebackup.databases.managed("agents/42.sqlite",
  { to: "/var/lib/app/agents/42.sqlite", durability: "cloud", adapter: adapter() },
  (db) => db.prepare("INSERT INTO steps (body) VALUES (?)").run("tests passed"));
```

The adapters roll back what the work did not commit and never commit for you.
For a scheduled job, await a foreground backup and choose the acknowledgement
you need:

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.backups.once("./export.sqlite", { wait: "cloud" });
```

Every operation takes a `signal`; aborting stops the local wait, and the
daemon may still finish. Errors are `LitebackupError` with `code`, `message`,
`status`, and `details`. See the [SDK reference](https://github.com/litebackup/sdk/blob/main/docs/sdk-reference.md),
[integrations](https://github.com/litebackup/sdk/blob/main/docs/sqlite-integrations.md), and
[migration notes](https://github.com/litebackup/sdk/blob/main/CHANGELOG.md).
