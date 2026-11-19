# Add Litebackup to a SQLite app or worker

There are two ways to use Litebackup from code. Pick by who owns the database.

| Your code | Use | What it does |
| --- | --- | --- |
| An app that keeps one database open on one machine | **Backup registration**: `backups.start(path)` at startup | Saves a continuous backup rule. The daemon backs up every change in the background. |
| A worker or agent that moves a database between machines | **Managed ownership**: `databases.managed(name, ...)` around the work | Acquires the database, opens it, runs your code, closes it, and releases it to the server or bucket. |

Never both for one database: an acquired database already syncs while you write.

## Register a backup at startup

Get the file path from the handle or configuration the app already has, and
register it on every start. Registration is idempotent: the same path and
settings return the saved rule, so every process, worker, and restart may call
it. Other settings for a saved path fail with `exists`, and the error's
`details.rule` is the saved rule. Stop it with `rules.stop(rule.id)` to change it.

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";
import { databasePath } from "@litebackup/sdk/better-sqlite3";

const db = new Database("data/app.sqlite");
await new Litebackup().backups.start(databasePath(db));
```

```python
import sqlite3

from litebackup import Litebackup
from litebackup.integrations.sqlite3 import database_path

connection = sqlite3.connect("data/app.sqlite")
with Litebackup() as litebackup:
    litebackup.backups.start(database_path(connection))
```

```go
dsn := "file:data/app.sqlite?_pragma=journal_mode(WAL)"
db, err := sql.Open("sqlite", dsn)
if err != nil {
    return err
}
path, err := sqlitedsn.Modernc(dsn)
if err != nil {
    return err
}
_, err = litebackup.StartBackup(ctx, path, nil)
```

The path helpers resolve relative paths against the working directory, decode
`file:` URIs the way the driver does, and refuse in-memory and temporary
databases, which have no file to back up.

### Django

```python
INSTALLED_APPS = [..., "litebackup.integrations.django"]
LITEBACKUP = {"databases": {"default": {}}}
```

The app registers each listed database in `AppConfig.ready()`, from its
`NAME`. Entry keys are `backups.start` options (`name`, `prefix`,
`maximum_local_databases`). Set `"enabled": False` for tests and image builds
that run without the daemon. A non-SQLite, in-memory, or test database is an
`ImproperlyConfigured` error. Before the first `migrate`, when the file does
not exist yet, the app creates the empty file SQLite would create on first
connect. See [examples/django](../examples/django).

### FastAPI and other ASGI apps

```python
from fastapi import FastAPI
from litebackup.integrations.fastapi import backup_lifespan

app = FastAPI(lifespan=backup_lifespan("/var/lib/app/main.sqlite", lifespan=existing_lifespan))
```

`backup_lifespan` runs an existing lifespan's startup first, so a database it
creates exists, then registers before the app serves, puts the client in the lifespan state as `litebackup`, and closes it on
exit. See [examples/fastapi](../examples/fastapi).

## Own a database for one run

```ts
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
await litebackup.databases.managed("agents/42.sqlite",
  { to: "/var/lib/app/agents/42.sqlite", durability: "cloud", adapter: adapter() },
  (db) => {
    db.exec("CREATE TABLE IF NOT EXISTS steps (body TEXT)");
    db.prepare("INSERT INTO steps (body) VALUES (?)").run("tests passed");
  });
```

```python
from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

with Litebackup() as litebackup, litebackup.databases.managed(
    "agents/42.sqlite", to="/var/lib/app/agents/42.sqlite", durability="cloud", adapter=adapter()
) as db:
    db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
    db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
    db.commit()
```

```go
err := litebackup.Managed(ctx, client, "agents/42.sqlite", &litebackup.ManagedArgs[*sql.DB]{
    To: "/var/lib/app/agents/42.sqlite", Durability: litebackup.DurabilityCloud, Adapter: litebackup.SQLAdapter("sqlite"),
}, func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", "tests passed")
    return err
})
```

**The built-in adapters roll back what the work did not commit, and never commit
for you.** Commit the changes the next machine should see. Python's `sqlite3`
opens a transaction on the first write, so call `commit()`.

A scope always closes and releases, even when the work fails or is cancelled:

- When `to` is given and nothing is there yet, the scope creates an empty file
  first, so the first run creates the database. Pass `create: false`
  (`create=False`, Go `RequireExisting: true`) to require a database the server
  or this machine already has.
- `durability` is required: `server` returns once the server has the final
  version, and `cloud` once the bucket has it too.
- `offload` removes the local file after release. It applies only when the work
  succeeded; a failed run keeps its copy for the retry.
- Closing and releasing run under their own `cleanupTimeout`, 10 minutes by
  default, even after the caller cancels.
- If closing fails, the scope **does not release**: a connection may still be
  open, so the database stays acquired by this machine and its file is left as
  it is. The error is `scope_cleanup_failed` with `details.step: "close"`.
  Close every connection, then call `databases.release(name)`.
- If releasing fails, the error is `scope_cleanup_failed` with
  `details.step: "release"` and the durability that was asked for.
- When the work and the cleanup both fail, both are raised, the work's error
  first: an `AggregateError` in TypeScript, an `ExceptionGroup` in Python, and
  `errors.Join` in Go. In async Python, a cancelled task still cleans up, then
  re-raises `CancelledError` with any cleanup failure attached.

See [agent state](sqlite-agent-state.md) for retries and concurrency.

## Supported drivers and frameworks

| Language | Driver or framework | Register a path | Managed adapter |
| --- | --- | --- | --- |
| TypeScript | better-sqlite3 | `databasePath(db, { schema? })` from `@litebackup/sdk/better-sqlite3` | `adapter(Database, options?)` |
| TypeScript | node:sqlite | `databasePath(db, { schema? })` from `@litebackup/sdk/node-sqlite` (Node 24+, `DatabaseSync.location()`) | `adapter(options?)` |
| TypeScript | Drizzle (better-sqlite3 or node:sqlite client) | `databasePath(db)` from `@litebackup/sdk/drizzle` | the client's adapter |
| Python | sqlite3 | `database_path(connection, schema="main")` | `adapter(**connect_options)` |
| Python | aiosqlite (`litebackup[aiosqlite]`) | `await database_path(connection)` | `adapter(**connect_options)` |
| Python | SQLAlchemy (`litebackup[sqlalchemy]`) | `database_path(engine \| async_engine \| url)` | open your own connections; pass a custom adapter |
| Python | Django (`litebackup[django]`) | the `litebackup.integrations.django` app | not applicable |
| Python | FastAPI, Starlette, any ASGI | `backup_lifespan(*paths)` | not applicable |
| Go | modernc.org/sqlite, mattn/go-sqlite3 | `sqlitedsn.Modernc(dsn)`, `sqlitedsn.Mattn(dsn)` | `SQLAdapter("sqlite")`, `SQLAdapter("sqlite3")` |
| Any | any other driver | the file path you open | write an adapter |

`schema` names an attached database. Every other driver works with the path
it opens: `backups.start("/var/lib/app/main.sqlite")`.

## Write an adapter

For registration, return the absolute path of the file the connection opens,
and refuse in-memory and temporary databases. Prefer the driver's documented
way to ask, such as `PRAGMA database_list`, over parsing its configuration.

For a managed scope, an adapter has two methods:

- `open(path)` returns a connection to the file at `path`.
- `close(connection)` rolls back an unfinished transaction, never commits,
  closes every connection it opened, and **fails when one may still be open**,
  such as a pooled connection still checked out. The scope releases only after
  `close` succeeds.

```ts
import type { ScopeAdapter } from "@litebackup/sdk";

interface MyConnection {
  inTransaction: boolean;
  rollback(): Promise<void>;
  close(): Promise<void>;
}
declare function openMyDriver(path: string): Promise<MyConnection>;

export const myAdapter: ScopeAdapter<MyConnection> = {
  open: (path) => openMyDriver(path),
  close: async (connection) => {
    if (connection.inTransaction) await connection.rollback();
    await connection.close();
  },
};
```

Python adapters implement `litebackup.ScopeAdapter` (or `AsyncScopeAdapter`
with async methods), and Go adapters implement `litebackup.ScopeAdapter[DB]`.
