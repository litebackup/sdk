# Restore SQLite to a point in time with Litebackup

Restore into a new file, inspect it, and then plan the app's cutover. Install a
[server](litebackup-server.md), join the [client](litebackup-client.md), and first register a backup:

```sh
litebackup start /var/lib/app/main.sqlite
```

Allow a sync to complete before expecting a restorable version. You can
perform the restore on another joined machine with suitable permissions.

## List versions and restore

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

const available = await litebackup.databases.versions("main.sqlite");
console.log(available);
await litebackup.databases.restore("main.sqlite", { path: "/tmp/main-restored.sqlite" });
```

```sh
litebackup versions main.sqlite
litebackup restore main.sqlite /tmp/main-restored.sqlite
sqlite3 /tmp/main-restored.sqlite 'PRAGMA integrity_check'
```

The destination directory must exist. The file must not exist. Litebackup
runs SQLite's `quick_check` before returning; a manual integrity check can be
part of a more extensive restore test.

## Select a time or version

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.databases.restore("main.sqlite", {
  path: "/tmp/main-before-migration.sqlite",
  time: "2026-11-15T09:30:00Z",
});
```

```sh
litebackup restore main.sqlite /tmp/main-before-migration.sqlite \
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
measure how long the restore test took. Keep the existing app file while deciding
whether to roll back. A restored file is a copy: it is not the origin of the
database's name.

## Roll back under the same name

To roll back, revert. A revert publishes a past version's content again as a
new head, one past the current head, as `git revert` adds a commit, so every
later version stays restorable:

```ts
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
const reverted = await litebackup.databases.revert("main.sqlite", { version: 120 });
console.log(`version ${reverted.sourceVersion} is version ${reverted.version} now`);
```

```sh
litebackup revert main.sqlite --version 120
```

Run it on the machine that writes the database, a backup's origin or an
acquired copy, with the app's SQLite connections closed: an open file is
refused with `still_open`, and a replica or a failover standby with
`invalid_argument`. A file holding writes the server lacks is refused with
`conflict` and left as it is; let them sync first. The client takes the
write lock when it does not hold it, puts the version in place of the
closed file, syncs it at once, and gives back a lock it took. When that
sync fails for any reason but a lost lock or a refused file, which are
final, the version stays in place and the client keeps the lock and syncs
again on its own; release an acquired database once it synced.
`--time` (`time`) picks the newest version at or before a time, as restore
does.

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
# LITEBACKUP_SECRET_ACCESS_KEY must contain the bucket's secret access key.
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
