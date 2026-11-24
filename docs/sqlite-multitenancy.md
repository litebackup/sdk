# One SQLite database per tenant with Litebackup

Separate SQLite files make tenant data easy to place, restore, and move
individually. Litebackup gives each file a stable remote name. Complete
[setup](litebackup-server.md) and [join your app machine](litebackup-client.md) first.

## Back up a directory

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

await litebackup.backups.start("/var/lib/app/tenants/*.sqlite", { prefix: "tenants/" });
```

```sh
litebackup start '/var/lib/app/tenants/*.sqlite' tenants/
```

The existing directory contains files such as `42.sqlite` and `73.sqlite`.
They become `tenants/42.sqlite` and `tenants/73.sqlite`. The rule picks up new
matching files without waiting on each tenant's first upload. Open those files
with your SQLite driver as usual.

Use application-generated, validated tenant IDs for filenames. Do not derive
paths directly from request input. Case is preserved, so `ACME.sqlite` and `acme.sqlite` are distinct backup names.

## Add tenants from code

For applications that provision databases explicitly, register each new file:

```ts
import { mkdir } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

export async function createTenant(id: string): Promise<void> {
  if (!/^[0-9]+$/.test(id)) throw new Error("Expected a numeric tenant ID");
  await mkdir("/var/lib/app/tenants", { recursive: true });
  const path = `/var/lib/app/tenants/${id}.sqlite`;
  const db = new DatabaseSync(path);
  try {
    db.exec("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)");
  } finally {
    db.close();
  }
  await litebackup.backups.start(path, { name: `tenants/${id}.sqlite` });
}
```

Use a directory rule or register individual files; you only need one.
The Node example requires 22.13+.

## Serve tenants from a pool

Every tenant file stays on local disk, and your app opens it with its SQLite
driver as usual. A pool keeps each tenant's connection open between requests,
so every request after the first runs at local SQLite speed, without code that
tracks open connections per tenant.

```ts
import { Litebackup } from "@litebackup/sdk";
import { adapter } from "@litebackup/sdk/node-sqlite";

const litebackup = new Litebackup();
const pool = litebackup.databases.pool({ adapter: adapter() });

export async function addNote(id: string, body: string): Promise<void> {
  await pool.use(`tenants/${id}.sqlite`, (db) => {
    db.prepare("INSERT INTO notes (body) VALUES (?)").run(body);
  });
}
```

```python
from litebackup import Litebackup
from litebackup.integrations.sqlite3 import adapter

litebackup = Litebackup()
pool = litebackup.databases.pool(adapter=adapter())


def add_note(tenant_id: str, body: str) -> None:
    with pool.use(f"tenants/{tenant_id}.sqlite") as db:
        db.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        db.commit()
```

```go
pool, err := litebackup.NewPool(client, &litebackup.PoolArgs[*sql.DB]{
    Adapter: litebackup.SQLAdapter("sqlite", nil),
})
if err != nil {
    return err
}
err = pool.Use(ctx, "tenants/42.sqlite", func(ctx context.Context, db *sql.DB) error {
    _, err := db.ExecContext(ctx, "INSERT INTO notes (body) VALUES (?)", "hello")
    return err
})
```

`use` takes the tenant's remote name and asks the client for its path, so
the app builds no paths from request input. It refuses a name no rule on
this machine covers. A complete Go program is in
[examples/goapp/tenants](../examples/goapp/tenants/main.go).

## Offload tenants to save disk

Keep every tenant local unless the disk cannot hold them. `maximumLocalDatabases`
on the rule offloads the least recently changed idle tenants past that count,
once your server holds their last version; the next use of one downloads the
whole database again, so set the count above the tenants you expect to be
active. Rule offload removes WAL databases, and rollback-journal ones only with
`offloadRollbackJournal`, your word that nothing keeps them open while idle.

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.backups.start("/var/lib/app/tenants/*.sqlite", {
  prefix: "tenants/",
  maximumLocalDatabases: 500,
});
```

With a pool, nothing else changes: under such a rule the pool closes a tenant
after 2 minutes unused (`idleClose`), so the rule can offload it, and brings an
offloaded tenant back on its next `use`. Under a rule with
`offloadRollbackJournal`, the pool closes a rollback-journal tenant after
each `use`, so it is never open while the client counts it idle; WAL tenants
stay open. An app that opens tenant files itself
must close them when idle, open them without creating a missing file, and call
`databases.acquire(name)` for one that was offloaded.

## Place tenants on multiple app machines

A database name has one writer. Either:

- Assign each tenant to one machine and route requests there. Keep stable
  `tenants/ID.sqlite` names so a tenant's identity survives a move.
- Use shard prefixes such as `shards/eu-1/ID.sqlite` when those are deliberately
  separate namespaces. Include the prefix when restoring or replicating.
- Lease tenants that move between machines from a pool (`pool.lease`), as in
  the [agent example](sqlite-agent-state.md): a lease keeps the tenant until
  it is idle for a minute, released, or the pool closes; another machine's
  lease with `lockTimeout` waits for that.

Do not start the same directory backup on multiple machines with independent
copies and expect them to merge. The second origin receives `busy`.
Do not use a different name merely to hide an ownership error.

For a planned migration, stop accepting writes, close the database, wait for
the current backup, then stop it. Acquire the stable remote name on the new
machine before opening it for writes. Route traffic only after acquisition
succeeds. If the tenant already uses acquire/release, use `release` for its
handoff. See [backup waits](sqlite-backup.md#wait-for-a-backup) for choosing when to
wait for the server or bucket.

## Replicate and restore tenants independently

```sh
# On a reporting machine joined to the same server:
litebackup replicate tenants/ /var/lib/app/reporting

# Recover one customer's database into a new file:
litebackup restore tenants/42.sqlite /tmp/tenant-42-restored.sqlite
```

The [replica guide](sqlite-replication.md) covers the namespace SDK methods and readiness.
Use a readonly join token scoped to the namespace for reporting where possible.
