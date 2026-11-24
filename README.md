# Litebackup

**Local SQLite, synced to your cloud.**

Back up SQLite to your own server and bucket, keep live readonly replicas,
and move databases between machines. Your app keeps using its SQLite driver.

Litebackup has three parts: a **server** you run, which keeps versions and
uploads them encrypted to **your bucket**, and a **client** on each app
machine, which the SDKs start for you and which backs up your SQLite files
while your app keeps writing to them.

https://github.com/user-attachments/assets/0a48622a-4a70-4f28-a99f-5cfa109ce421

[Website](https://litebackup.com) · [Pricing](https://litebackup.com/pricing) ·
[Documentation](docs/README.md) · [API reference](docs/sdk-reference.md)

## Quickstart

### 1. Install your server

On a fresh Debian 13 or Ubuntu 26.04 LTS VM with a public IP and inbound TCP
443 open (2 vCPUs, 2–4 GB RAM, and an SSD is plenty to start), run:

```sh
curl -fsSL https://litebackup.com/server.sh | bash
```

<a href="https://cloud.digitalocean.com/droplets/new?image=debian-13-x64&amp;refcode=13cb93d1ec16"><img src="docs/assets/digitalocean.svg" width="20" height="20" alt=""> Create a Debian 13 Droplet on DigitalOcean</a> (referral link), or use AWS, Google Cloud, Hetzner, or any other provider.

Open the link it prints, claim your instance, add an S3, R2, Google Cloud
Storage, or S3-compatible bucket, and save your recovery kit or set a recovery
passphrase; keep it off the VM. Then open **Machines → Add machine** and copy
the token.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/console-dark.png">
  <img src="docs/assets/console.png" alt="Litebackup console showing database backup status, versions, and the machine writing each database">
</picture>

[Server setup, networking, and sizing →](docs/litebackup-server.md) ·
[Bucket providers and permissions →](docs/sqlite-cloud-backup.md)

### 2. Install the SDK on your app machine

```sh
# TypeScript, Node.js 22+
npm install @litebackup/sdk better-sqlite3

# Python 3.11+
pip install litebackup

# Go: the SDK runs the litebackup command, so install it too
curl -fsSL https://litebackup.com/cli.sh | bash
go mod init example.com/app && go get github.com/litebackup/sdk/litebackup modernc.org/sqlite

# Every language: the join token from Machines → Add machine, or an API key
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
```

The npm package and the Linux and macOS wheels include the client; it starts
and joins your server on first use.

### 3. Back up a database

Register the file your app opens. `backups.start` returns once the rule is
saved; the client then backs up every change in the background. Call it on
every start: the same path and settings return the saved rule.

```ts
import { mkdirSync } from "node:fs";
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

mkdirSync("data", { recursive: true });
const path = "data/main.sqlite";
const db = new Database(path);
db.exec("CREATE TABLE IF NOT EXISTS notes (body TEXT)");
db.prepare("INSERT INTO notes (body) VALUES (?)").run("hello");

const litebackup = new Litebackup();
const rule = await litebackup.backups.start(path);
const status = await litebackup.databases.sync(rule.name); // optional: wait until your server has it
console.log(`${rule.name} version ${status.syncedServer.version} is on your server`);
```

```python
import os
import sqlite3

from litebackup import Litebackup

os.makedirs("data", exist_ok=True)
path = "data/main.sqlite"
db = sqlite3.connect(path)
db.execute("CREATE TABLE IF NOT EXISTS notes (body TEXT)")
db.execute("INSERT INTO notes (body) VALUES (?)", ("hello",))
db.commit()

with Litebackup() as litebackup:
    rule = litebackup.backups.start(path)
    status = litebackup.databases.sync(rule.name)  # optional: wait until your server has it
    print(f"{rule.name} version {status.synced_server.version} is on your server")
```

```go
// main.go; run it with: go run .
package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"os"

	"github.com/litebackup/sdk/litebackup"
	_ "modernc.org/sqlite"
)

func main() {
	ctx := context.Background()
	if err := os.MkdirAll("data", 0o755); err != nil {
		log.Fatal(err)
	}
	path := "data/main.sqlite"
	db, err := sql.Open("sqlite", path)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	if _, err := db.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS notes (body TEXT)"); err != nil {
		log.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, "INSERT INTO notes (body) VALUES (?)", "hello"); err != nil {
		log.Fatal(err)
	}

	rule, err := litebackup.StartBackup(ctx, path, nil)
	if err != nil {
		log.Fatal(err)
	}
	client, err := litebackup.Default(ctx) // the client the package functions share
	if err != nil {
		log.Fatal(err)
	}
	status, err := client.Sync(ctx, rule.Name) // optional: wait until your server has it
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("%s version %d is on your server\n", rule.Name, status.SyncedServer.Version)
}
```

Or, with no code changes: `litebackup start data/main.sqlite`. When your code
has only a connection, not its path, see [what the path helpers do](docs/sqlite-integrations.md#what-the-path-helpers-do).

### 4. Restore it

Restore the newest version, or one from any time your retention keeps, into a
new file, on this or any joined machine:

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.databases.restore("main.sqlite", { path: "data/restored.sqlite" });
console.log(new Database("data/restored.sqlite").prepare("SELECT body FROM notes").all());
```

```python
with Litebackup() as litebackup:
    litebackup.databases.restore("main.sqlite", path="data/restored.sqlite")
print(sqlite3.connect("data/restored.sqlite").execute("SELECT body FROM notes").fetchall())
```

```sh
litebackup restore main.sqlite "${PWD}/data/restored.sqlite"
sqlite3 data/restored.sqlite 'SELECT body FROM notes'
```

Restore checks the new file and never overwrites an existing one. Pass `time`
(`--time 2026-11-15T09:30:00Z`) or `version` for an earlier version.
To roll back under the same name, revert: `databases.revert(name, { version })`
(`litebackup revert main.sqlite --version 2`) publishes that version's
content again as the newest version, and every later version stays
restorable.
[Point-in-time restore and server recovery →](docs/sqlite-restore.md)

## What else you can do

| Task | Guide |
| --- | --- |
| Back up every tenant's database with one glob, including new ones | [Multitenancy](docs/sqlite-multitenancy.md) |
| Move an agent's or customer's database between machines, one writer at a time | [Agent state](docs/sqlite-agent-state.md) |
| Keep live readonly replicas on other machines | [Replication](docs/sqlite-replication.md) |
| Keep writing when an app server goes down | [High availability](https://litebackup.com/failover) |
| Back up once from a cron job, CI, or a serverless function | [One-shot jobs](docs/sqlite-backup-script.md), [cron](docs/sqlite-cron-backup.md), [serverless](docs/sqlite-serverless.md) |
| Use your driver or framework: better-sqlite3, node:sqlite, Drizzle, sqlite3, aiosqlite, SQLAlchemy, Django, FastAPI, Go | [Integrations](docs/sqlite-integrations.md) |
| Run the client as a service, in Docker, or under its own user | [Client runtime](docs/litebackup-client.md), [Docker](docs/sqlite-docker.md) |

## Frequently asked questions

### How does Litebackup affect performance and safety?

Litebackup uses SQLite's own code and APIs to back up your database without
writing to it. Your app keeps its existing driver and the extensively tested
official SQLite engine. The `litebackup` background process handles continuous
backup, so your app doesn't wait for uploads. Add one SDK call, or use the CLI
with no application changes.

[How backups work](docs/sqlite-backup.md) · [SQLite's testing](https://www.sqlite.org/testing.html)

### Where does my data live?

Your app's SQLite files stay on its disk. Backups live on your Litebackup server
and in buckets you own on S3, R2, Google Cloud Storage, or another S3-compatible store.

### What if I lose the server?

Recover a replacement server from your bucket using your recovery kit or
passphrase. See [server recovery](docs/sqlite-restore.md#recover-after-losing-the-server).

### What does it cost?

Plans cover a set number of databases and machines. See [pricing](https://litebackup.com/pricing)
for details.

### When should I wait for a backup?

`backups.start` only saves the rule; uploads run in the background, and status
shows how far each database got. Foreground jobs (`backups.once`, `litebackup
backup`) and the release that ends a pool's lease wait for your server by default. That is enough
in most cases: your server keeps the version and archives it to your bucket
after its archive delay. Until it does, losing the server's disk and the app's copy at
the same time loses those changes, so wait for `cloud` when a job is the only
copy and its machine goes away afterwards.

### What is LITEBACKUP_API_KEY?

The one credential a machine needs, then @ and your server address: a join
token (`JOIN_TOKEN@HOST`), which the client exchanges once for a key of the
machine, or an API key (`API_KEY@HOST`), which it uses as it is. Persistent
sessions save their key and use it until the variable holds a different
credential. Fresh foreground jobs use a temporary identity. See
[client credentials](docs/litebackup-client.md#join-and-register).

## Go further

| Guide | What it covers |
| --- | --- |
| [Live SQLite backup](docs/sqlite-live-backup.md) | Back up a running database and handle WAL files |
| [Recover deleted rows](docs/sqlite-deleted-rows.md) | Restore selected records without replacing the whole database |
| [SQLite in Docker](docs/sqlite-docker.md) | Pinned CLI installation in a Dockerfile, persistent volumes, and a runnable Compose example |
| [Integrations](docs/sqlite-integrations.md) | better-sqlite3, node:sqlite, Drizzle, sqlite3, aiosqlite, SQLAlchemy, Django, FastAPI, Go drivers |
| [API reference](docs/sdk-reference.md) | TypeScript, Python, and Go operations, options, errors, and status |
| [Client runtime](docs/litebackup-client.md) | Sessions, credentials, service installs, Docker, shutdown |
| [Storage providers](docs/sqlite-cloud-backup.md) | S3, R2, GCS, and other S3-compatible buckets |
| [Troubleshooting](docs/sqlite-backup-errors.md) | Join failures, name conflicts, timeouts, and diagnostics |
