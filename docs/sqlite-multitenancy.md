# One SQLite database per tenant with Litebackup

Separate SQLite files make tenant data easy to place, restore, and move
individually. Litebackup gives each file a stable remote name. Complete
[setup](litebackup-server.md) and [join your app machine](litebackup-client.md) first.

## Back up a directory

```ts
import { startBackup } from "@litebackup/sdk";

await startBackup("/var/lib/app/tenants/*.sqlite", { prefix: "tenants/" });
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
import { startBackup } from "@litebackup/sdk";

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
  await startBackup(path, { name: `tenants/${id}.sqlite` });
}
```

Use a directory rule or register individual files; you only need one.
The Node example requires 22.13+.

## Place tenants on multiple app machines

A database name has one writer. Either:

- Assign each tenant to one machine and route requests there. Keep stable
  `tenants/ID.sqlite` names so a tenant's identity survives a move.
- Use shard prefixes such as `shards/eu-1/ID.sqlite` when those are deliberately
  separate namespaces. Include the prefix when restoring or replicating.
- Use acquire/release around finite tenant jobs that move between machines,
  as in the [agent example](sqlite-agent-state.md).

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
litebackup replicate --prefix tenants --to /var/lib/app/reporting

# Recover one customer's database into a new file:
litebackup restore tenants/42.sqlite --to /tmp/tenant-42-restored.sqlite
```

The [replica guide](sqlite-replication.md) covers the namespace SDK methods and readiness.
Use a readonly join token scoped to the namespace for reporting where possible.
