# Persistent SQLite state for AI agents

An agent can keep memory, tasks, and results in a local SQLite file, hand it
back at the end of a run, and continue on another machine. Set up a
[server](litebackup-server.md) and [join each execution machine](litebackup-client.md) first.

## One run: acquire, open, work, close, release

`databases.managed` does the whole run. It acquires the database, which
creates it on the first run or downloads its latest version, opens it, runs your code, closes
it, and releases it with the durability you choose.

```ts
import { mkdir } from "node:fs/promises";
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
const id = "42";
await mkdir("/var/lib/app/agents", { recursive: true });

await litebackup.databases.managed(
  `agents/${id}.sqlite`,
  { to: `/var/lib/app/agents/${id}.sqlite`, durability: "cloud", offload: true, adapter: adapter() },
  (db) => {
    db.exec("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, body TEXT)");
    db.prepare("INSERT INTO memory (body) VALUES (?)").run("Tests passed on main");
  },
);
```

```python
import os

from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

os.makedirs("/var/lib/app/agents", exist_ok=True)
with Litebackup() as litebackup, litebackup.databases.managed(
    "agents/42.sqlite", to="/var/lib/app/agents/42.sqlite", durability="cloud", offload=True, adapter=adapter()
) as db:
    db.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO memory (body) VALUES (?)", ("Tests passed on main",))
    db.commit()
```

```go
err := litebackup.Managed(ctx, client, "agents/42.sqlite", &litebackup.ManagedArgs[*sql.DB]{
    To: "/var/lib/app/agents/42.sqlite", Durability: litebackup.DurabilityCloud, Offload: true,
    Adapter: litebackup.SQLAdapter("sqlite"),
}, func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO memory (body) VALUES (?)", "Tests passed on main")
    return err
})
```

**What the work does not commit is rolled back.** The adapters never commit
for you. Statements outside a transaction commit as they run in `node:sqlite`
and `database/sql`; Python's `sqlite3` opens a transaction on the first write,
so call `commit()`.

Use `durability: "cloud"` when the machine may disappear after the run. On a
persistent machine, `"server"` returns sooner. `offload` removes the local
file once the bucket has the released version, and only after work that
succeeded. Complete examples: [TypeScript](../typescript/examples/agent-state.ts),
[Python](../examples/python/agent_state.py), [Go](../examples/go-app/agent/main.go).

## When something fails

- **The work fails or is cancelled.** The scope still closes and releases, without
  offload, and raises the work's error. Committed progress is saved.
- **Closing fails.** The scope does not release: a connection may still be
  open, and another machine must not write while this one still can. The
  database stays acquired here and its file is left as it is. The error is
  `scope_cleanup_failed` with `details.step` `"close"`; close every
  connection, then call `databases.release(name)`.
- **Releasing fails** (for example `still_open`, or the server is unreachable).
  The error is `scope_cleanup_failed` with `details.step` `"release"`. The
  file is intact; release again before another machine needs it.
- **More than one fails.** Both are raised, the work's error first.

Closing and releasing run under their own `cleanupTimeout`, 10 minutes by
default, even after a cancel.

## CLI equivalent

After joining as the same OS user that runs the agent:

```sh
mkdir -p /var/lib/app/agents
touch /var/lib/app/agents/42.sqlite
litebackup acquire agents/42.sqlite --to /var/lib/app/agents/42.sqlite
sqlite3 /var/lib/app/agents/42.sqlite \
  "CREATE TABLE IF NOT EXISTS memory (body TEXT); INSERT INTO memory VALUES ('Tests passed');"
litebackup release agents/42.sqlite --wait cloud --offload --timeout 30m
```

Each `sqlite3` command closes its connection before `release`. `release` fails
with `still_open` while another connection or process has the database open.

## Concurrency and retry rules

- Serialize runs of the same agent in your scheduler. Different agents can
  run concurrently on separate files.
- If acquiring fails with `in_use`, another machine has the database. Retry
  with backoff, keeping the same name. The SDKs never retry acquire or release
  themselves.
- After a lost lease, inspect any conflict copy before deciding how to merge
  application work. Litebackup keeps divergent writes; it does not merge SQL
  transactions across machines.
- If a release fails, fail the job and keep the local files.

`onLost: "block"` blocks local writes after ownership is lost; the default
saves divergent writes as a conflict copy. Use idempotency keys for external
actions so retries do not repeat them. Do not also register a continuous
`backups.start()` for an acquired database; acquiring already syncs while you write.

## Manage local disk

`offload: true` on the scope, or `release(name, { offload: true })`, removes
the local file once the bucket holds the released version. `offload(name)`
offloads an already released database. The next acquire downloads it again.
Close and discard old SQLite handles before reacquiring a file that may be
replaced.

For maintenance on release, pass `maintenance: true`. For bulk backup rules,
`maximumLocalDatabases` controls offloading idle databases; it is separate
from your agent's ownership and scheduling.

See [integrations](sqlite-integrations.md) for adapters, [Temporal](sqlite-temporal.md)
for placing file access in activities, and [Go](sqlite-go.md) for complete Go programs.
