# Litebackup for TypeScript

Back up SQLite to your Litebackup server and bucket. Install the SDK and a
driver with `npm install @litebackup/sdk better-sqlite3` on Linux or macOS
with Node.js 22 or later.
In **Machines → Add machine**, copy the value for `LITEBACKUP_API_KEY` and set
it in your application's environment. An API key followed by @ and your server
address works too.

```ts
import { mkdirSync } from "node:fs";
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

mkdirSync("data", { recursive: true });
const path = "data/main.sqlite";
const db = new Database(path);
const litebackup = new Litebackup();
const rule = await litebackup.backups.start(path);
console.log(rule.id);
```

The rule is saved locally before listing and uploads finish. Call
`backups.start` on every start: the same path and settings return the saved
rule. Keep the daemon and its local state on persistent storage.

Code that has the handle but not its path can ask SQLite which file the
handle opened, which is what the path helpers do:

```ts
import { mkdirSync } from "node:fs";
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

mkdirSync("data", { recursive: true });
const db = new Database("data/main.sqlite");
const file = db.prepare("SELECT file FROM pragma_database_list WHERE name = 'main'").pluck().get() as string;
const litebackup = new Litebackup();
await litebackup.backups.start(file); // an empty file means in-memory: nothing to back up
```

Each driver has its own subpath export with `databasePath(db)`, which runs that
query, refuses in-memory databases with `invalid_argument`, and never guesses
what a handle is:

| Import | Path of a handle | Pool adapter |
| --- | --- | --- |
| `@litebackup/sdk/better-sqlite3` | `databasePath(db, { schema? })` | `adapter(Database, options?)` |
| `@litebackup/sdk/node-sqlite` | `databasePath(db, { schema? })` | `adapter(options?)` |
| `@litebackup/sdk/drizzle` | `databasePath(drizzleDb)` | the client's adapter |

With any other driver, pass the filename, a `file:` URL, or the result of that query.

The SDK needs Node.js 22 or later. `@litebackup/sdk/node-sqlite` needs
`node:sqlite`, available without a flag from Node.js 22.13, and its
`databasePath` needs Node.js 24 for `DatabaseSync.location()`; on older
releases, pass the path you opened. `@litebackup/sdk/drizzle` loads
`node:sqlite` only for a node:sqlite client, so better-sqlite3 clients work on
any supported Node.js.

For databases that move between machines, a pool leases each one: the first
call acquires its write lock and opens it, later calls reuse both, and the
pool releases it after a minute unused (`idleRelease`), on `release`, or on
`close`. Another machine's lease with `lockTimeout` gets the lock once this
one is released:

```ts
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
const pool = litebackup.databases.pool({ adapter: adapter() });
// The first lease creates data/agents/ and an empty file when nothing is there yet.
await pool.lease("agents/42.sqlite", { path: "data/agents/42.sqlite" },
  (db) => {
    db.exec("CREATE TABLE IF NOT EXISTS steps (body TEXT NOT NULL)");
    db.prepare("INSERT INTO steps (body) VALUES (?)").run("tests passed");
  });
await pool.close();
```

`pool.use(name, work)` keeps a backup rule's database open between calls
instead, and brings it back first when the rule offloaded it. Under a rule
saved with `offloadRollbackJournal`, a rollback-journal database closes as
soon as no `use` call runs, so the rule can offload it safely. The adapters
open existing files only, refusing a missing one with `not_found`.
The adapters roll back what the work did not commit and never commit for you.
For a scheduled job, await a foreground backup. It returns once your server has
the version, which is enough in most cases; pass `durability: "cloud"` to wait for the
bucket too:

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

const path = "export.sqlite";
const db = new Database(path);
db.exec("CREATE TABLE IF NOT EXISTS reports (body TEXT NOT NULL)");
db.prepare("INSERT INTO reports (body) VALUES (?)").run("nightly");
db.close();

const litebackup = new Litebackup();
await litebackup.backups.once(path);
```

Every operation takes a `signal`; aborting stops the local wait, and the
daemon may still finish. Errors are `LitebackupError` with `code`, `message`,
`status`, and `details`. See the [SDK reference](https://github.com/litebackup/sdk/blob/main/docs/sdk-reference.md),
[integrations](https://github.com/litebackup/sdk/blob/main/docs/sqlite-integrations.md), and
[changelog](https://github.com/litebackup/sdk/blob/main/CHANGELOG.md).
