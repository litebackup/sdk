# Restore SQLite to a point in time with Litebackup

Restore into a new file, inspect it, and then plan the app's cutover. Install a
[server](litebackup-server.md), join the [client](litebackup-client.md), and first register a backup:

```sh
litebackup backup /var/lib/app/main.sqlite
```

Allow a sync to complete before expecting a restorable version. You can
perform the restore on another joined machine with suitable permissions.

## List versions and restore

```ts
import { restore, versions } from "@litebackup/sdk";

const available = await versions("main.sqlite");
console.log(available);
await restore("main.sqlite", { to: "/tmp/main-restored.sqlite" });
```

```sh
litebackup versions main.sqlite
litebackup restore main.sqlite --to /tmp/main-restored.sqlite
sqlite3 /tmp/main-restored.sqlite 'PRAGMA integrity_check'
```

The destination directory must exist. The file must not exist. Litebackup
runs SQLite's `quick_check` before returning; a manual integrity check can be
part of a more extensive restore drill.

## Select a time or version

```ts
await restore("main.sqlite", {
  to: "/tmp/main-before-migration.sqlite",
  time: "2026-11-15T09:30:00Z",
});
```

```sh
litebackup restore main.sqlite --to /tmp/main-before-migration.sqlite \
  --time 2026-11-15T09:30:00Z
```

Time selects the newest retained version at or before the timestamp. It does
not reconstruct a transaction between captured versions. Supply an RFC 3339
UTC timestamp. Use `version` / `--version` for a version returned by
`versions`; do not supply both time and version. Retention determines which
versions remain available.

For a tenant, use its full name, for example `tenants/42.sqlite`. For a job
using the scheduled prefix, use `scheduled/main.sqlite`.

## Check before replacing app data

Query application tables in the restored file, validate expected records, and
measure how long the drill took. Keep the existing app file while deciding
whether to cut over. Stop application writes and close all SQLite handles
before replacing a live database; account for its WAL and SHM files and the
client's ownership state. A new restored file is not automatically registered
as the origin of the original remote name.

If you only need a few records, [recover deleted SQLite rows](sqlite-deleted-rows.md)
with an insert from the restored copy into the current database.

## Recover after losing the server

A surviving bucket plus the recovery kit's encryption key or your recovery
passphrase can rebuild server metadata from the versions archived in your bucket.

Before recovery, stop the old server and ensure it cannot resume writing to
the bucket. Recover into an **empty data directory** on the replacement host.
Install the compatible CLI, inspect `litebackup server recover --help`, and
supply the original provider, bucket, prefix, and credentials.

For an S3 bucket, a recovery command has this shape:

```sh
# On the replacement server, read secrets from your recovery storage.
# LITEBACKUP_BUCKET_SECRET_ACCESS_KEY must contain the bucket's secret access key.
litebackup server recover \
  --data-directory /var/lib/litebackup-recovered \
  --provider aws --region us-east-1 --bucket YOUR_BUCKET \
  --access-key-id YOUR_ACCESS_KEY_ID \
  --encryption-key-file /secure/recovery/encryption.key
```

Use `--passphrase-file /secure/recovery/passphrase` instead of the
`--encryption-key-file` option if you saved a recovery passphrase. Include
`--prefix` if the original bucket used one. R2 and generic S3 providers need
their original `--endpoint` too.

Recovery is distinct from claiming a fresh empty server. Coordinate the
replacement instance's launcher data directory with the recovered directory;
keep it stopped until recovery finishes. API keys are not in buckets and
must be created again. Complete instance setup, recreate machine credentials
as required, and verify restores before resuming writes. Keep the original
recovery material until you have verified the replacement.
