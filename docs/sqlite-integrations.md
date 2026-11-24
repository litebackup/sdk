# Add Litebackup to a SQLite app or worker

There are two ways to use Litebackup from code. Pick by who owns the database.
Either way every database stays on local disk and your SQLite code runs
unchanged; offloading local files is an option you turn on.

| Your code | Use | What it does |
| --- | --- | --- |
| An app that keeps one database open on one machine | **Backup registration**: `backups.start(path)` at startup | Saves a continuous backup rule. The daemon backs up every change in the background. |
| A worker or agent that moves a database between machines | **A lease from a pool**: `pool.lease(name, ...)` around the work | Acquires the database on its first call, keeps it open for later calls, and releases it to the server after a minute unused, on `release`, or when the pool closes. |

Never both for one database: an acquired database already syncs while you write.

## Register a backup at startup

Get the file path from the handle or configuration the app already has, and
register it on every start. Registration is idempotent: the same path and
settings return the saved rule, so every process, worker, and restart may call
it. Other settings for a saved path fail with `rule_exists`, and the error's
`details.rule` is the saved rule. Stop it with `rules.stop(rule.id)` to change it.

Pass the file your app opens:

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

const path = "data/app.sqlite";
const db = new Database(path);
await new Litebackup().backups.start(path); // relative paths resolve against the working directory
```

```python
import sqlite3

from litebackup import Litebackup

path = "data/app.sqlite"
connection = sqlite3.connect(path)
with Litebackup() as litebackup:
    litebackup.backups.start(path)
```

```go
path := "data/app.sqlite"
db, err := sql.Open("sqlite", "file:"+path+"?_pragma=journal_mode(WAL)")
if err != nil {
    return err
}
_, err = litebackup.StartBackup(ctx, path, nil)
```

### What the path helpers do

When code gets a connection but not the path it was opened with, ask SQLite
which file the connection opened. `PRAGMA database_list` names every attached
database and its file; an empty file means an in-memory or temporary database,
which has nothing to back up:

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

const db = new Database("data/app.sqlite");
const litebackup = new Litebackup();
const file = db.prepare("SELECT file FROM pragma_database_list WHERE name = 'main'").pluck().get() as string;
if (!file) throw new Error("an in-memory database has no file to back up");
await litebackup.backups.start(file);
```

```python
(file,) = connection.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()
if not file:
    raise ValueError("an in-memory database has no file to back up")
litebackup.backups.start(file)
```

```go
var file string
if err := db.QueryRowContext(ctx, "SELECT file FROM pragma_database_list WHERE name = 'main'").Scan(&file); err != nil {
    return err
}
if file == "" {
    return errors.New("an in-memory database has no file to back up")
}
_, err = litebackup.StartBackup(ctx, file, nil)
```

The helpers run this query (`node:sqlite`'s uses `DatabaseSync.location()`, its
equivalent), raise `invalid_argument` for in-memory and temporary databases,
and take a `schema` for an attached database:

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";
import { databasePath } from "@litebackup/sdk/better-sqlite3";

const db = new Database("data/app.sqlite");
const litebackup = new Litebackup();
await litebackup.backups.start(databasePath(db));
```

```python
from litebackup.integrations.sqlite3 import database_path

litebackup.backups.start(database_path(connection))
```

In Go, `sqlitedsn` reads the file out of a DSN before anything is opened, the
way the driver parses it, including `file:` URIs and their parameters:

```go
dsn := "file:data/app.sqlite?_pragma=journal_mode(WAL)"
path, err := sqlitedsn.Modernc(dsn) // sqlitedsn.Mattn for github.com/mattn/go-sqlite3
if err != nil {
    return err
}
_, err = litebackup.StartBackup(ctx, path, nil)
```

### Django

```python
INSTALLED_APPS = [..., "litebackup.integrations.django"]
LITEBACKUP = {"databases": {"default": {}}}
```

The app registers each listed database in `AppConfig.ready()`, from its
`NAME`. Entry keys are `backups.start` options (`name`, `prefix`,
`maximum_local_databases`, `offload_durability`). Set `"enabled": False` for tests and image builds
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

## Keep databases open in a pool

A pool keeps each database open between calls, so every call after the first
runs at local SQLite speed. `use` is for databases a backup rule on this
machine covers; `lease` is for a database that moves between machines.

```ts
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
const pool = litebackup.databases.pool({ adapter: adapter() });
await pool.lease("agents/42.sqlite", { path: "/var/lib/app/agents/42.sqlite" }, (db) => {
  db.exec("CREATE TABLE IF NOT EXISTS steps (body TEXT)");
  db.prepare("INSERT INTO steps (body) VALUES (?)").run("tests passed");
});
await pool.close();
```

```python
from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

with Litebackup() as litebackup, litebackup.databases.pool(adapter=adapter()) as pool:
    with pool.lease("agents/42.sqlite", path="/var/lib/app/agents/42.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
        db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
        db.commit()
```

```go
pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[*sql.DB]{
    Adapter: litebackup.SQLAdapter("sqlite", nil),
})
if err != nil {
    return err
}
err = pool.Lease(ctx, "agents/42.sqlite", &litebackup.LeaseArgs{
    Path: "/var/lib/app/agents/42.sqlite",
}, func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", "tests passed")
    return err
})
err = errors.Join(err, pool.Close(ctx))
```

**The built-in adapters roll back what the work did not commit, and never commit
for you.** Commit the changes the next machine should see. Python's `sqlite3`
opens a transaction on the first write, so call `commit()`.

A lease keeps its lock until the pool releases it:

- When `path` is given and nothing is there yet, the first lease creates an
  empty file first, so the first run creates the database. Pass
  `requireExisting: true` (`require_existing=True`, Go `RequireExisting: true`)
  to require a database the server or this machine already has.
- With `durability`, a call returns once your server, or the bucket with
  `cloud`, holds what it committed, without releasing. The release that ends
  the lease waits for the server by default.
- The pool releases the lease after `idleRelease` (a minute unused by
  default), on `release(name)`, or on `close()`; it never gives it up because
  another machine waits. Another machine's lease with `lockTimeout` tries
  again until then.
- `offload` removes the local file when the lease ends, unless the last call
  failed; a failed call keeps its copy for the retry.
- Closing and releasing share one `cleanupTimeout`, 10 minutes by default,
  and run even after the caller cancels. The release gets what closing left.
- If closing fails, the pool **does not release**: a connection may still be
  open, so the database stays acquired by this machine and its file is left as
  it is. The error is a `LeaseCleanupError` with `step` `"close"`. Close every
  connection, then call `pool.release(name)`.
- If releasing fails, the error is a `LeaseCleanupError` with `step`
  `"release"` and the durability that was asked for.
- Several failures at once are raised together: an `AggregateError` in
  TypeScript, an `ExceptionGroup` in Python, and `errors.Join` in Go.

### What a lease does

The same run without a pool: create an empty file for a new database,
acquire it, open it with your driver, do the work, close it, and release it.
Release only after the close succeeded, so no other machine writes while a
connection may still be open.

```ts
import { mkdir, writeFile } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
const name = "agents/42.sqlite";
const path = "/var/lib/app/agents/42.sqlite";
await mkdir("/var/lib/app/agents", { recursive: true });
await writeFile(path, "", { flag: "a" }); // an empty file becomes a new database, or the server's head replaces it

const { database } = await litebackup.databases.acquire(name, { path });
const db = new DatabaseSync(database.path);
try {
  db.exec("CREATE TABLE IF NOT EXISTS steps (body TEXT)");
  db.prepare("INSERT INTO steps (body) VALUES (?)").run("tests passed");
} finally {
  db.close(); // if closing throws, the database stays acquired
  await litebackup.databases.release(name); // returns once your server has the final version
}
```

```python
import os
import sqlite3

from litebackup import Litebackup

name, path = "agents/42.sqlite", "/var/lib/app/agents/42.sqlite"
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "a").close()  # an empty file becomes a new database, or the server's head replaces it

with Litebackup() as litebackup:
    acquired = litebackup.databases.acquire(name, path=path)
    db = sqlite3.connect(acquired.database.path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
        db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
        db.commit()
    finally:
        db.close()  # if closing raises, the database stays acquired
        litebackup.databases.release(name)  # returns once your server has the final version
```

```go
name, path := "agents/42.sqlite", "/var/lib/app/agents/42.sqlite"
if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
    return err
}
// An empty file becomes a new database, or the server's head replaces it.
file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o644)
if err != nil {
    return err
}
file.Close()

acquired, err := client.Acquire(ctx, name, &litebackup.AcquireArgs{Path: path})
if err != nil {
    return err
}
db, err := sql.Open("sqlite", acquired.Database.Path)
if err != nil {
    return err
}
_, workErr := db.ExecContext(ctx, "INSERT INTO steps (body) VALUES (?)", "tests passed")
if err := db.Close(); err != nil {
    return errors.Join(workErr, err) // the database stays acquired
}
// Release even when ctx was cancelled; it returns once your server has the final version.
_, err = client.Release(context.WithoutCancel(ctx), name, nil)
return errors.Join(workErr, err)
```

A pool adds what this leaves out: the lock and the open connection kept for
later calls, a release after it is idle, offload only after a call
that succeeded, a cleanup timeout that holds after cancellation, rollback of
what the work did not commit, and `LeaseCleanupError`s that say how to
recover.

See [agent state](sqlite-agent-state.md) for retries and concurrency.

## Supported drivers and frameworks

| Language | Driver or framework | Register a path | Pool adapter |
| --- | --- | --- | --- |
| TypeScript | better-sqlite3 | `databasePath(db, { schema? })` from `@litebackup/sdk/better-sqlite3` | `adapter(Database, options?)` |
| TypeScript | node:sqlite | `databasePath(db, { schema? })` from `@litebackup/sdk/node-sqlite` (Node 24+, `DatabaseSync.location()`) | `adapter(options?)` |
| TypeScript | Drizzle (better-sqlite3 or node:sqlite client) | `databasePath(db)` from `@litebackup/sdk/drizzle` | the client's adapter |
| Python | sqlite3 | `database_path(connection, schema="main")` | `adapter(**connect_options)` |
| Python | aiosqlite (`litebackup[aiosqlite]`) | `await database_path(connection)` | `adapter(**connect_options)` |
| Python | SQLAlchemy (`litebackup[sqlalchemy]`) | `database_path(connection \| engine \| url)`, `await async_database_path(async_connection \| async_engine)`; pass a connection for an engine with a custom creator | open your own connections; pass a custom adapter |
| Python | Django (`litebackup[django]`) | the `litebackup.integrations.django` app | not applicable |
| Python | FastAPI, Starlette, any ASGI | `backup_lifespan(*paths)` | not applicable |
| Go | modernc.org/sqlite, mattn/go-sqlite3 | `DatabasePath(ctx, db)`, or `sqlitedsn.Modernc(dsn)`, `sqlitedsn.Mattn(dsn)` for a DSN | `SQLAdapter("sqlite", nil)`, `SQLAdapter("sqlite3", nil)`, with `SQLAdapterArgs{DSN}` for driver options |
| Any | any other driver | the file path you open | write an adapter |

`schema` names an attached database. Every other driver works with the path
it opens: `backups.start("/var/lib/app/main.sqlite")`.

## Write an adapter

For registration, return the absolute path of the file the connection opens,
and refuse in-memory and temporary databases. Prefer the driver's documented
way to ask, such as `PRAGMA database_list`, over parsing its configuration.

For a pool, an adapter has two methods:

- `open(path)` returns a connection to the existing file at `path` and **never
  creates one**: a missing file is an offloaded copy the pool brings back, so
  fail with `not_found` instead, as a `file:` URI with `mode=rw` does.
- `close(connection)` rolls back an unfinished transaction, never commits,
  closes every connection it opened, and **fails when one may still be open**,
  such as a pooled connection still in use. The pool releases a lease only
  after `close` succeeds.

```ts
import type { Adapter } from "@litebackup/sdk";

interface MyConnection {
  inTransaction: boolean;
  rollback(): Promise<void>;
  close(): Promise<void>;
}
declare function openMyDriver(path: string): Promise<MyConnection>;

export const myAdapter: Adapter<MyConnection> = {
  open: (path) => openMyDriver(path),
  close: async (connection) => {
    if (connection.inTransaction) await connection.rollback();
    await connection.close();
  },
};
```

Python adapters implement `litebackup.Adapter` (or `AsyncAdapter` with async
methods), and Go adapters implement `litebackup.Adapter[DB]`.
