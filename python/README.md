# Litebackup for Python

Back up, replicate, and restore SQLite databases with your Litebackup server
and bucket. Python 3.11 or later on Linux or macOS.

```sh
pip install litebackup
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
```

`LITEBACKUP_API_KEY` is the value from **Machines → Add machine**, or an API
key followed by @ and your server address. Wheels for
Linux (glibc and musl) and macOS, on x64 and arm64, include the `litebackup`
client, which the SDK starts on first use. On other platforms pip installs
the source package, which uses an installed `litebackup` command: install it
with `curl -fsSL https://litebackup.com/cli.sh | bash`, or set `LITEBACKUP_PATH`.

## Back up an app's database

```python
import os
import sqlite3

from litebackup import Litebackup

os.makedirs("data", exist_ok=True)
path = "data/app.sqlite"
connection = sqlite3.connect(path)
with Litebackup() as litebackup:
    rule = litebackup.backups.start(path)
    print(rule.id, rule.name)
```

Code that has the connection but not its path can ask SQLite which file it
opened. `database_path(connection)` from `litebackup.integrations.sqlite3` does
this and raises `invalid_argument` for an in-memory database:

```python
import os
import sqlite3

from litebackup import Litebackup

os.makedirs("data", exist_ok=True)
connection = sqlite3.connect("data/app.sqlite")
with Litebackup() as litebackup:
    (file,) = connection.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()
    if file:  # an empty file means in-memory: nothing to back up
        litebackup.backups.start(file)
```

`backups.start` returns once the daemon saved the rule; uploads run in the
background. Call it on every start: the same path and settings return the
saved rule, and other settings raise `LitebackupError` with code `rule_exists` and
the saved rule in `details["rule"]`.

| Integration | Extra | Use |
| --- | --- | --- |
| `litebackup.integrations.sqlite3` | | `database_path(connection, schema="main")`, `adapter()` |
| `litebackup.integrations.aiosqlite` | `aiosqlite` | `await database_path(connection)`, `adapter()` |
| `litebackup.integrations.sqlalchemy` | `sqlalchemy` | `database_path(connection \| engine \| url)`, `await async_database_path(async_connection \| async_engine)`; for an engine with a custom `creator`, pass a connection |
| `litebackup.integrations.django` | `django` | Add to `INSTALLED_APPS` and set `LITEBACKUP = {"databases": {"default": {}}}` |
| `litebackup.integrations.fastapi` | | `FastAPI(lifespan=backup_lifespan("/var/lib/app/main.sqlite"))` |
| `litebackup.release` | `release` | `find_release(metadata)` verifies signed release metadata |

## Lease a database that moves between machines

```python
from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

with Litebackup() as litebackup, litebackup.databases.pool(adapter=adapter()) as pool:
    with pool.lease("agents/42.sqlite", path="data/agents/42.sqlite", durability="server") as db:
        db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
        db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
        db.commit()
```

The first lease acquires the database's write lock, creating an empty file at
`path` when nothing is there, and opens it. The pool keeps the lock and the
open connection for later leases of the same name, which run one at a time,
until no lease used it for `idle_release` (a minute by default; 0 keeps it), or
you call `pool.release(name)` or `pool.close()`. Each release closes the
connection and returns once your server has the final version.

Your app coordinates handovers between machines: while one machine keeps the
lease, another machine's lease fails with `lock_unavailable`, or with
`lock_timeout=30` waits up to 30 seconds and succeeds once the first releases
it. A lease on a database whose lock this machine lost meanwhile raises
`lock_lost` before its block runs; the next lease acquires it again.

With `durability="server"` a lease returns once your server has what the block
committed; `"cloud"` waits for the bucket too. The adapter opens existing files
only and rolls back what the block did not commit; it never commits for you. A
block that raises keeps the lease, and the release that ends it does not
offload. A failed close does not release: the database stays acquired and the
error is a `LeaseCleanupError` (`lease_cleanup_failed`). Several failures
raise an `ExceptionGroup`.

For a database a backup rule on this machine covers, `pool.use(name)` yields
its connection, bringing back an offloaded copy first and keeping it open
between calls. A rollback-journal database under a rule saved with
`offload_rollback_journal` is closed as soon as its last `use` block exits
instead, so the rule never offloads it under an open connection.

## asyncio

`AsyncLitebackup` has the same groups, awaited, and async iterators. The
aiosqlite adapter below needs its extra: `pip install 'litebackup[aiosqlite]'`.

```python
import asyncio

from litebackup import AsyncLitebackup
from litebackup.integrations.aiosqlite import adapter


async def main() -> None:
    async with AsyncLitebackup() as litebackup:
        async for database in litebackup.databases.all():
            print(database.name, database.state)
        async with (
            litebackup.databases.pool(adapter=adapter()) as pool,
            pool.lease("agents/42.sqlite", path="data/agents/42.sqlite") as db,
        ):
            await db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
            await db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
            await db.commit()


asyncio.run(main())
```

Cancelling a task stops its local wait; the daemon may still finish the
operation, and `asyncio.CancelledError` propagates. A cancelled lease keeps
the lock, and the release that ends it does not offload; a block that catches
the cancellation and returns still counts as cancelled. `close()` waits for
active blocks, and a cancelled `close()` keeps releasing; await it again to
see its result.

## Everything else

The groups are `backups`, `rules`, `databases`, `replicas`,
`events`, `session`, and `failovers`, with keyword-only arguments. Durations
are seconds or `timedelta`. Results are frozen dataclasses with the API's
field names. `close()` never stops the daemon. See the
[SDK reference](https://github.com/litebackup/sdk/blob/main/docs/sdk-reference.md)
and [integrations](https://github.com/litebackup/sdk/blob/main/docs/sqlite-integrations.md).
