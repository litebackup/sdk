# Litebackup for Python

Back up, replicate, and restore SQLite databases with your Litebackup server
and bucket. Python 3.11 or later on Linux or macOS.

```sh
pip install litebackup
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
```

`LITEBACKUP_TOKEN` is the value from **Machines → Add machine**. Wheels for
Linux (glibc and musl) and macOS, on x64 and arm64, include the `litebackup`
client, which the SDK starts on first use. On other platforms pip installs
the source package, which uses an installed `litebackup` command: install it
with `curl -fsSL https://litebackup.com/cli.sh | bash`, or set `LITEBACKUP_PATH`.

## Back up an app's database

```python
import sqlite3

from litebackup import Litebackup
from litebackup.integrations.sqlite3 import database_path

connection = sqlite3.connect("data/app.sqlite")
with Litebackup() as litebackup:
    rule = litebackup.backups.start(database_path(connection))
    print(rule.id, rule.name)
```

`backups.start` returns once the daemon saved the rule; uploads run in the
background. Call it on every start: the same path and settings return the
saved rule, and other settings raise `LitebackupError` with code `exists` and
the saved rule in `details["rule"]`.

| Integration | Extra | Use |
| --- | --- | --- |
| `litebackup.integrations.sqlite3` | | `database_path(connection, schema="main")`, `adapter()` |
| `litebackup.integrations.aiosqlite` | `aiosqlite` | `await database_path(connection)`, `adapter()` |
| `litebackup.integrations.sqlalchemy` | `sqlalchemy` | `database_path(engine \| async_engine \| url)` |
| `litebackup.integrations.django` | `django` | Add to `INSTALLED_APPS` and set `LITEBACKUP = {"databases": {"default": {}}}` |
| `litebackup.integrations.fastapi` | | `FastAPI(lifespan=backup_lifespan("/var/lib/app/main.sqlite"))` |
| `litebackup.release` | `release` | `find_release(metadata)` verifies signed release metadata |

## Own a database for one run

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

The scope acquires the database, yields the connection, then closes it and
releases it with `durability`. The adapter rolls back what the block did not
commit; it never commits for you. A failed block still closes and releases. A
failed close does not release: the database stays acquired and the error is
`scope_cleanup_failed`. Several failures raise an `ExceptionGroup`, the block's
error first.

## asyncio

`AsyncLitebackup` has the same groups, awaited, and async iterators:

```python
import asyncio

from litebackup import AsyncLitebackup
from litebackup.integrations.aiosqlite import adapter


async def main() -> None:
    async with AsyncLitebackup() as litebackup:
        async for database in litebackup.databases.iter_all():
            print(database.name, database.state)
        async with litebackup.databases.managed(
            "agents/42.sqlite", to="/var/lib/app/agents/42.sqlite", durability="server", adapter=adapter()
        ) as db:
            await db.execute("INSERT INTO steps (body) VALUES (?)", ("tests passed",))
            await db.commit()


asyncio.run(main())
```

Cancelling a task stops its local wait; the daemon may still finish the
operation, and `asyncio.CancelledError` propagates. A cancelled managed scope
still closes and releases under its own `cleanup_timeout` first.

## Everything else

The groups are `backups`, `rules`, `databases`, `replicas`, `conflicts`,
`events`, `session`, and `failovers`, with keyword-only arguments. Durations
are seconds or `timedelta`. Results are frozen dataclasses with the API's
field names. `close()` never stops the daemon. See the
[SDK reference](https://github.com/litebackup/sdk/blob/main/docs/sdk-reference.md)
and [integrations](https://github.com/litebackup/sdk/blob/main/docs/sqlite-integrations.md).
