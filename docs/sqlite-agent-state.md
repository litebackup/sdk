# Persistent SQLite state for AI agents

An agent can keep memory, tasks, and results in a local SQLite file, release
it at the end of a run, and resume on another machine. Set up a
[server](litebackup-server.md) and [join each execution machine](litebackup-client.md) first.

## Acquire, write, close, release

This Node.js 22.13+ example creates a local file if needed. `acquire` then
creates the remote database or downloads its latest version.

```ts
import { appendFile, mkdir } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { acquire, release } from "@litebackup/sdk";

const id = "42";
const name = `agents/${id}.sqlite`;
const path = `/var/lib/app/agents/${id}.sqlite`;
await mkdir("/var/lib/app/agents", { recursive: true });
await appendFile(path, "");

await acquire(name, { to: path, timeout: "2m" });
try {
  const db = new DatabaseSync(path);
  try {
    db.exec("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, body TEXT)");
    db.prepare("INSERT INTO memory (body) VALUES (?)").run("Tests passed on main");
  } finally {
    db.close();
  }
} finally {
  // Save committed progress even if the agent fails.
  await release(name, { wait: "cloud", offload: true, timeout: "30m" });
}
```

Use `wait: "cloud"` when the machine may disappear after the run. On a
persistent machine, `wait: "server"` returns sooner. Either way, offloading
only removes the local file after the bucket has it.

Do not also register a continuous `backup()` for an acquired database. The
acquisition already syncs while you write.

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

Each `sqlite3` command closes its connection before `release`. In a real agent,
put release in cleanup that runs after all database handles close and make
release failure fail the job. `release` can return `still_open` while another
connection or process has the database open.

## Concurrency and retry rules

- Serialize runs of the same agent in your scheduler. Different agents can
  run concurrently on separate files.
- If `acquire` returns `in_use`, another machine has the database. Retry with
  backoff, keeping the same name. Wait for acquisition before opening SQLite.
- After a lost lease, inspect any conflict copy before deciding how to merge
  application work. Litebackup preserves divergent writes; it does not merge
  SQL transactions across machines.
- If a wait or release fails, fail the job and keep the local files.

`onLost: "block"` blocks local writes after ownership is lost; the default
saves divergent writes as a conflict. Use idempotency keys for external
actions so retries do not repeat them.

## Manage local disk

`release(name, { offload: true })` requests removal once the bucket holds the
released version. `offload(name)` explicitly offloads an already released
database. A later `acquire(name, { to: path })` downloads it again. Close and
discard old SQLite handles before reacquiring a file that may be replaced.

For maintenance on release, pass `maintenance: true` to `acquire`. For bulk
backup rules, `maximumLocalDatabases` controls offloading idle databases; it
is separate from your agent's ownership and scheduling policy.

See [Temporal](sqlite-temporal.md) for placing file access in activities and
[Go](sqlite-go.md) for the same ownership flow in Go.
