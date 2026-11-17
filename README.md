# Litebackup

**Local SQLite, synced to your cloud.**

Back up SQLite to your own server and bucket, keep live readonly replicas,
and move databases between machines. Your app keeps using its SQLite driver.

```ts
import Database from "better-sqlite3";
import { backup } from "@litebackup/sdk";

const db = new Database("/var/lib/app/main.sqlite");
backup(db);
```

Or add backups to an existing app with the CLI:

```sh
litebackup backup /var/lib/app/main.sqlite
```

https://github.com/user-attachments/assets/0a48622a-4a70-4f28-a99f-5cfa109ce421

[Website](https://litebackup.com) · [Pricing](https://litebackup.com/pricing) ·
[Documentation](docs/README.md) · [API reference](docs/sdk-reference.md)

## Install your server

Use a Linux VM on AWS, Google Cloud, DigitalOcean, Hetzner, or another provider.

For a small deployment, start with **2 vCPUs, 2–4 GB RAM, and an SSD** on a fresh
Ubuntu 24.04 LTS or Debian 13 VM, with a public IP and inbound TCP 443 open.
Size the disk for your databases, write volume, and recent history.

SSH into the VM and run:

```sh
curl -fsSL https://litebackup.com/server.sh | bash
```

1. Open the link the installer prints and claim your instance.
2. Add an **Amazon S3, Cloudflare R2, Google Cloud Storage, or S3-compatible bucket**.
3. Save your recovery kit or set a recovery passphrase, and choose retention.
4. Open **Machines → Add machine** to get a join token.

The installer gives you an HTTPS address and a web console. Your server
encrypts backups before uploading them. Keep your recovery kit or passphrase
off the VM; you'll need it if you lose the server.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/console-dark.png">
  <img src="docs/assets/console.png" alt="Litebackup console showing database backup status, versions, and the machine writing each database">
</picture>

[Server setup, networking, and sizing →](docs/litebackup-server.md)
[Bucket providers and permissions →](docs/sqlite-cloud-backup.md)

## Connect your app

**TypeScript / Node.js 22+**

```sh
npm install @litebackup/sdk
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
```

The package includes the Linux and macOS client (x64 and arm64). It starts
and joins automatically on first use. Keep npm optional dependencies enabled.

`LITEBACKUP_TOKEN` is the **join token plus server address** from Machines →
Add machine, not an API key. The client joins once and saves its own machine key.

**CLI / Go**

Install the command on each app machine, then join as the user running your app:

```sh
curl -fsSL https://litebackup.com/cli.sh | sudo bash
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
litebackup up "$LITEBACKUP_TOKEN"
```

```sh
go get github.com/litebackup/sdk/litebackup
```

For backups that resume after a reboot, [install the client as a service](docs/litebackup-client.md#join-and-register).
The [client guide](docs/litebackup-client.md) also covers sessions, permissions, and containers.

## Back up without slowing startup

Pass a path or a `better-sqlite3` handle. `backup()` returns immediately;
`ready` confirms the local client has started the backup, without waiting for an upload.

The opening example uses `better-sqlite3` (`npm install better-sqlite3`);
you can also pass a filename and use any SQLite driver. In Go:

```go
import "github.com/litebackup/sdk/litebackup"

// ctx is your application's context; the SQLite file already exists.
_, err := litebackup.Backup(ctx, "/var/lib/app/main.sqlite", litebackup.BackupOptions{})
if err != nil {
    return err
}
```

Your app writes to its local SQLite files as usual, and Litebackup syncs changes
in the background. Keep those files on a persistent disk or volume so they
remain available across restarts and redeploys. Use `wait("server")` or
`wait("cloud")` when a script or handoff should finish after its changes
reach the server or bucket.

[Continuous backup, globs, and durability →](docs/sqlite-backup.md)
[Complete Go examples →](docs/sqlite-go.md)

## A database per tenant

Keep customers in separate SQLite files and give their backups stable names:

```ts
import { backup } from "@litebackup/sdk";

const tenants = backup("/var/lib/app/tenants/*.sqlite", { prefix: "tenants/" });
void tenants.ready.catch(console.error);
```

```sh
litebackup backup '/var/lib/app/tenants/*.sqlite' --prefix tenants/
```

`42.sqlite` becomes `tenants/42.sqlite`; new matching files are picked up too.
Each name has one writer. Use a prefix per shard; a second machine trying
to back up the same name gets `name_in_use`.

[Tenant routing, naming, and moving a tenant between machines →](docs/sqlite-multitenancy.md)

## A database per agent

Acquire a database before the agent opens it. Close SQLite and release it when
the run finishes; the next machine acquires the latest released version.

```ts
import { appendFile, mkdir } from "node:fs/promises";
import { acquire, release } from "@litebackup/sdk";

await mkdir("/var/lib/app/agents", { recursive: true });
const path = "/var/lib/app/agents/42.sqlite";
await appendFile(path, ""); // create the file if needed; keep existing contents
await acquire("agents/42.sqlite", { to: path });
// Run the agent with path using any SQLite driver, then close every connection.
await release("agents/42.sqlite", { offload: true });
```

Wait for `acquire()` before writing. `release()` syncs the last changes to
the server; `offload: true` removes the local file once it's in your bucket.

[Complete agent example, retries, and local disk management →](docs/sqlite-agent-state.md)

## Read from another machine

On a second machine joined to the same server:

```ts
import { replicate } from "@litebackup/sdk";

await replicate("main.sqlite", { to: "/var/lib/app/reporting.sqlite" });
// The first version is local. Open reporting.sqlite with your driver in readonly mode.
```

```sh
litebackup replicate main.sqlite --to /var/lib/app/reporting.sqlite
# Or follow a whole namespace, including new databases:
litebackup replicate --prefix tenants --to /var/lib/app/replicas
```

The replica keeps updating in the background and can be behind the writer.

[Readonly connections, namespace replication, and stopping replicas →](docs/sqlite-replication.md)

## Restore a previous version

```ts
import { restore, versions } from "@litebackup/sdk";

console.log(await versions("main.sqlite"));
await restore("main.sqlite", {
  to: "/tmp/main-restored.sqlite",
  time: new Date("2026-11-15T09:30:00Z"),
});
```

```sh
litebackup restore main.sqlite --to /tmp/main-restored.sqlite \
  --time 2026-11-15T09:30:00Z
```

Restore checks the new file and never overwrites an existing one. Retention
settings determine which versions you can restore.

[Restore drills, point-in-time selection, and server recovery →](docs/sqlite-restore.md)

## Back up once, then finish

In a script or scheduled job, wait for the bucket before reporting success,
then stop the backup. Local files and remote backups stay.

```ts
import { backup } from "@litebackup/sdk";

for (const path of ["/var/lib/app/exports/main.sqlite", "/var/lib/app/exports/audit.sqlite"]) {
  const run = backup(path, { prefix: "scheduled/" });
  await run.ready;
  try {
    await run.wait("cloud", { signal: AbortSignal.timeout(30 * 60_000) });
  } finally {
    await run.stop();
  }
}
```

Use files not covered by a running backup, such as these exports. If your app
already backs up a file, wait on its existing handle and leave it running.
Each file is backed up separately; the list isn't one atomic snapshot.

[One-shot jobs and bounded waits →](docs/sqlite-backup-script.md) ·
[Cron and Kubernetes CronJob →](docs/sqlite-cron-backup.md) ·
[Temporal activities and schedules →](docs/sqlite-temporal.md)

## Go further

| Guide | What it covers |
| --- | --- |
| [Live SQLite backup](docs/sqlite-live-backup.md) | Back up a running database and handle WAL files |
| [Recover deleted rows](docs/sqlite-deleted-rows.md) | Restore selected records without replacing the whole database |
| [SQLite in Docker](docs/sqlite-docker.md) | Persistent volumes, client state, and a runnable Compose example |
| [API reference](docs/sdk-reference.md) | TypeScript and Go operations, options, errors, and status |
| [Client runtime](docs/litebackup-client.md) | Sessions, credentials, service installs, Docker, shutdown |
| [Storage providers](docs/sqlite-cloud-backup.md) | S3, R2, GCS, and other S3-compatible buckets |
| [Troubleshooting](docs/sqlite-backup-errors.md) | Join failures, name conflicts, timeouts, and diagnostics |
| [Development](docs/sdk-development.md) | Build, tests, API generation, and release verification |
