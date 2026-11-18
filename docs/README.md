# SQLite backup guides for Litebackup

Start with the [README](../README.md), or pick a guide below.

| Task | Guide |
| --- | --- |
| Install a server and connect machines | [Server](litebackup-server.md), [client runtime](litebackup-client.md) |
| Connect a bucket | [Storage providers](sqlite-cloud-backup.md) |
| Back up SQLite continuously | [SQLite backup](sqlite-backup.md) |
| Back up while the app keeps writing | [Live SQLite backup and WAL](sqlite-live-backup.md) |
| Keep SQLite data across container replacements | [SQLite in Docker](sqlite-docker.md) |
| Give each customer a database | [Tenants](sqlite-multitenancy.md) |
| Move agent state between machines | [Agents](sqlite-agent-state.md) |
| Read a database on another machine | [Replicas](sqlite-replication.md) |
| Restore SQLite to an earlier time | [Point-in-time restore](sqlite-restore.md) |
| Recover records after a delete | [Recover deleted SQLite rows](sqlite-deleted-rows.md) |
| Back up a list and exit | [One-shot jobs](sqlite-backup-script.md) |
| Schedule backups | [Cron and Kubernetes CronJob](sqlite-cron-backup.md) |
| Run backups as retried activities | [Temporal](sqlite-temporal.md) |
| Integrate from Go | [Go examples](sqlite-go.md) |
| Look up a method or error | [API](sdk-reference.md), [troubleshooting](sqlite-backup-errors.md) |
| Work on this SDK | [Development](sdk-development.md) |

TypeScript examples use ESM. Examples importing `node:sqlite` require Node.js
22.13+ (Node 24 is used for the runnable examples); the SDK itself supports
Node 22+. Install your preferred SQLite driver separately if using another.
All database directories must exist and be accessible to the local client.

Use the [API reference](sdk-reference.md) for method names and options, and the
[client guide](litebackup-client.md) for sessions, permissions, and shutdown.

[Agent fleets](sqlite-fleet.md) · [Serverless workers](sqlite-serverless.md) · [Direct HTTP API](http-api.md)
