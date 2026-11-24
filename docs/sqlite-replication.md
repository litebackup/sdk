# How to replicate SQLite databases with Litebackup

Keep a local copy for reporting or search indexing. Install a
[server](litebackup-server.md) and [join both machines](litebackup-client.md). On the origin, first
register an existing file:

```sh
litebackup start /var/lib/app/main.sqlite
```

The default remote name is `main.sqlite`. On a second machine, use a readonly
join token where possible and create a writable destination directory.

## Replicate one database

```ts
import { mkdir } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

await mkdir("/var/lib/app/reporting", { recursive: true });
await litebackup.replicas.start("main.sqlite", { path: "/var/lib/app/reporting/main.sqlite", timeout: "2m" });
const db = new DatabaseSync("/var/lib/app/reporting/main.sqlite", { readOnly: true });
try {
  console.log(db.prepare("PRAGMA quick_check").get());
} finally {
  db.close();
}
```

```sh
litebackup replicate main.sqlite /var/lib/app/reporting/main.sqlite --timeout 2m
```

The call returns when the first version is local. Further versions stream in
the background, so the replica can be behind the writer.

## Follow a namespace

Follow all databases under a prefix, including new ones:

```ts
import { Litebackup } from "@litebackup/sdk";

const client = new Litebackup();
await client.replicas.startPrefix("tenants", { directory: "/var/lib/app/reporting" });
// tenants/42.sqlite lands at /var/lib/app/reporting/42.sqlite.
console.log(await client.replicas.list());
client.close(); // replicas keep streaming in the daemon
```

```go
rule, err := client.ReplicatePrefix(ctx, "tenants", &litebackup.ReplicatePrefixArgs{
    Directory: "/var/lib/app/reporting",
})
if err != nil {
    return err
}
_ = rule
```

```sh
litebackup replicate tenants/ /var/lib/app/reporting
```

Prefix registration returns before the files download. Check `replicas.list()`
/ `Replicas(ctx)` and the database's status before opening it. An empty prefix
follows every database the client can access. Prefix replicas need the
litebackup client and SDK of the same release
([release compatibility](sdk-reference.md#release-compatibility)).

## Protect replica files

| Mode | Behavior |
| --- | --- |
| `lock` (default) | Holds the SQLite write lock; accidental app writes fail with `SQLITE_BUSY` while readers continue |
| `detect` | Detects local writes and rebases the replica; use when lock protection cannot be used |
| `none` | Relies on your app to open the database readonly |

Always open application connections in readonly mode. Do not treat `detect`
as a way to send writes upstream.

## Stop replication

```ts
import { Litebackup } from "@litebackup/sdk";

const client = new Litebackup();
await client.replicas.stop("main.sqlite");
await client.replicas.stopPrefix("tenants", "/var/lib/app/reporting");
client.close();
```

```sh
litebackup replicate stop main.sqlite
litebackup replicate stop tenants/ /var/lib/app/reporting
```

Go uses `StopReplica(ctx, name)` and `StopReplicaPrefix(ctx, prefix, &litebackup.StopReplicaPrefixArgs{Directory: directory})`.
Files stay at their last versions, with Litebackup's write protection removed.
