# How to recover deleted SQLite rows from a backup

Restore a copy from before the delete, inspect it, and copy the missing rows
back into the current database. This keeps the rest of your app's current
records in place. You need a retained backup from before the delete.

## Restore the version you need

With a [joined Litebackup client](litebackup-client.md), list the database's
versions and restore the one just before the change:

```sh
litebackup versions main.sqlite
litebackup restore main.sqlite --to /tmp/main-before-delete.sqlite \
  --time 2026-11-15T09:30:00Z
```

Use the full backup name if it has a prefix, such as `tenants/42.sqlite`.
The restore path must be new. You can also choose `--version` from the list;
see [SQLite restore](sqlite-restore.md#select-a-time-or-version).

## Inspect the rows before copying

These examples use an `orders` table with columns `id`, `customer_id`, and
`total_cents`. Replace the table, columns, and IDs with the records you intend
to recover. Confirm the current table still has a compatible schema.

```sh
sqlite3 -readonly /tmp/main-before-delete.sqlite \
  'SELECT id, customer_id, total_cents FROM orders WHERE id IN (101, 102);'
```

Check that these are the records you want, and that the IDs aren't being
used for different records in the current database.

## Copy only missing rows in a transaction

Run this as the user that owns the app database:

```sh
sqlite3 -bail /var/lib/app/main.sqlite <<'SQL'
.timeout 5000
PRAGMA foreign_keys = ON;
ATTACH DATABASE '/tmp/main-before-delete.sqlite' AS recovered;
BEGIN IMMEDIATE;
INSERT INTO main.orders (id, customer_id, total_cents)
SELECT old.id, old.customer_id, old.total_cents
FROM recovered.orders AS old
WHERE old.id IN (101, 102)
  AND NOT EXISTS (SELECT 1 FROM main.orders AS current WHERE current.id = old.id);
COMMIT;
DETACH DATABASE recovered;
SQL
```

The transaction inserts the selected rows only when their IDs are absent.
It leaves existing rows unchanged. `-bail` stops the shell if a statement
fails; an uncommitted transaction rolls back when the connection closes.
The app can keep reading, but other writers may wait while this short write
transaction runs.

If the rows depend on other tables, restore the parent records first. Review
triggers and application rules before applying the insert. Avoid `INSERT OR
REPLACE` here: the goal is to add missing records without replacing current
ones. SQLite's [ATTACH documentation](https://www.sqlite.org/lang_attach.html)
explains how one connection reads both files.

## Verify the result

```sh
sqlite3 -readonly /var/lib/app/main.sqlite \
  'SELECT id, customer_id, total_cents FROM orders WHERE id IN (101, 102);'
```

Keep the restored copy until you've checked the result in your app. For a
whole-database recovery instead, follow the [restore guide](sqlite-restore.md).
To capture future changes, [enable continuous SQLite backup](sqlite-backup.md).
