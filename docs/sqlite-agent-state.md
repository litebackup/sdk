# Persistent SQLite state for AI agents

An agent can keep memory, tasks, and results in a local SQLite file, hand it
back at the end of a run, and continue on another machine. Set up a
[server](litebackup-server.md) and [join each execution machine](litebackup-client.md) first.

## Lease the agent's database from a pool

A pool's `lease` runs your code with the agent's database. The first lease on
a machine acquires it, which creates it on the first run or downloads its
latest version, and opens it. The pool keeps the write lock and the open
connection for later steps on that machine, so each step after the first
runs at local SQLite speed. Closing the pool releases the database once your
server has the final version, so the next machine continues from there.

```ts
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
const pool = litebackup.databases.pool({ adapter: adapter() });

await pool.lease("agents/42.sqlite", { path: "/var/lib/app/agents/42.sqlite", lockTimeout: "1m" }, (db) => {
  db.exec("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, body TEXT)");
  db.prepare("INSERT INTO memory (body) VALUES (?)").run("Tests passed on main");
});
await pool.close();
```

```python
from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

with Litebackup() as litebackup, litebackup.databases.pool(adapter=adapter()) as pool:
    with pool.lease("agents/42.sqlite", path="/var/lib/app/agents/42.sqlite", lock_timeout=60) as db:
        db.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO memory (body) VALUES (?)", ("Tests passed on main",))
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
    Path: "/var/lib/app/agents/42.sqlite", LockTimeout: time.Minute,
}, func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO memory (body) VALUES (?)", "Tests passed on main")
    return err
})
err = errors.Join(err, pool.Close(ctx))
```

**What the work does not commit is rolled back.** The adapters never commit
for you. Statements outside a transaction commit as they run in `node:sqlite`
and `database/sql`; Python's `sqlite3` opens a transaction on the first write,
so call `commit()`.

**Wait for the server when a step must be safe before you go on.** A lease
with `durability: "server"` (Python `durability="server"`, Go
`Durability: litebackup.DurabilityServer`) returns once your server holds what
the step committed, without releasing; `"cloud"` waits for the bucket too.
Without it the step syncs in the background, and the release at the end waits
for the server.

**Hand the database over.** This pool releases a lease after a minute
unused (`idleRelease`), on `pool.release(name)`, or on `pool.close()`; it is
never asked to by another machine, so coordinate handovers in your
scheduler. A lease on another machine with `lockTimeout` tries again until
then; without it that lease fails at once with `lock_unavailable`. Complete
examples: [TypeScript](../typescript/examples/agent-state.ts),
[Python](../examples/python/agent_state.py), [Go](../examples/goapp/agent/main.go).

## When something fails

- **The step fails or is cancelled.** The lease stays and the step's error is
  raised. Committed progress is saved; the release that ends the lease does
  not offload.
- **The lock is lost** (it lapsed, or an admin broke it). The next step
  checks the lock first and fails with `lock_lost` without running. The local file keeps its writes and
  the copy is conflicted.
- **Closing fails when the lease ends.** The pool does not release: a
  connection may still be open, and another machine must not write while this
  one still can. The database stays acquired here and its file is left as it
  is. The error is a `LeaseCleanupError` with `step` `"close"`; close every
  connection, then call `pool.release(name)`.
- **Releasing fails** (for example `still_open`, or the server is unreachable).
  The error is a `LeaseCleanupError` with `step` `"release"`. The file is
  intact; release again before another machine needs it. A release the pool
  ran on its own goes to `onReleaseError` and is tried again at its next check
  and on `close()`.

Closing and releasing share one `cleanupTimeout`, 10 minutes by default, and
run even after a cancel.

## CLI equivalent

After joining as the same OS user that runs the agent:

```sh
mkdir -p /var/lib/app/agents
touch /var/lib/app/agents/42.sqlite
litebackup acquire agents/42.sqlite /var/lib/app/agents/42.sqlite
sqlite3 /var/lib/app/agents/42.sqlite \
  "CREATE TABLE IF NOT EXISTS memory (body TEXT); INSERT INTO memory VALUES ('Tests passed');"
litebackup release agents/42.sqlite --offload --timeout 30m
```

Each `sqlite3` command closes its connection before `release`. `release` fails
with `still_open` while another connection or process has the database open.

## Concurrency and retry rules

- Serialize runs of the same agent in your scheduler. Different agents can
  run concurrently on separate files.
- If acquiring fails with `lock_unavailable`, another machine has the database.
  Lease with `lockTimeout` to wait until it is released, or retry later with
  the same name.
- After a lost lease, the local file keeps its writes and the copy is
  conflicted; nothing takes the lock back on its own. An acquire resumes the
  copy, its local writes included, while the head is still the version it
  last synced. Once another machine moved the head, Litebackup does not
  merge SQL transactions across machines: the acquire refuses the copy with
  `conflict` until you decide, and `discardLocal` replaces it with the head.
- If a release fails, fail the job and keep the local files.

A lost lock stops uploads and leaves the copy conflicted, its file as it was
and still writable. Use idempotency keys for external
actions so retries do not repeat them. Do not also register a continuous
`backups.start()` for an acquired database; acquiring already syncs while you write.

## Offload to save disk

Every database stays local by default, so the next step on the same machine
needs no download. Offload removes the local file and suits agents that run
rarely: the next lease downloads the whole database again. `offload: true` on
a lease removes the file when the lease ends, once the server holds the
released version, or the bucket with `cloud` durability, unless the last step
failed. `release(name, { offload: true })` and `offload(name)` do the same for
databases you acquire yourself.

For maintenance on release, pass `maintenance: true`. For bulk backup rules,
`maximumLocalDatabases` controls offloading idle databases; it is separate
from your agent's ownership and scheduling.

See [integrations](sqlite-integrations.md) for adapters, [Temporal](sqlite-temporal.md)
for placing file access in activities, and [Go](sqlite-go.md) for complete Go programs.
