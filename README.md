<h1 align="center">Litebackup</h1>

<p align="center"><strong>Local SQLite, synced to your cloud.</strong></p>

<p align="center">
Continuous backup, point-in-time restore, and live replicas for SQLite,<br>
on your own server and bucket, while your app keeps its SQLite driver.
</p>

<p align="center">
<a href="https://litebackup.com">Website</a> ·
<a href="#getting-started">Getting started</a> ·
<a href="docs/README.md">Documentation</a> ·
<a href="docs/sdk-reference.md">API reference</a> ·
<a href="https://litebackup.com/pricing">Pricing</a>
</p>

https://github.com/user-attachments/assets/0a48622a-4a70-4f28-a99f-5cfa109ce421

## Why Litebackup

- **Your app stays on SQLite.** It reads and writes a local file at local
  speed, with the driver it already uses.
- **Every change is backed up.** A background client sends changes to your
  server as your app writes, so a restore brings back everything up to the
  last sync.
- **Restore any version.** Bring back the newest copy, or the database as it
  was at any time your retention keeps, on any machine.
- **Your bucket, your keys.** Versions are encrypted and archived to S3, R2,
  Google Cloud Storage, or any S3-compatible bucket you own.
- **More than backup.** Keep live readonly replicas, move a database between
  machines one writer at a time, and fail over when a server goes down.

## Common questions

**Does it slow my app down?**\
Backups read the database through SQLite's own API, in a separate client
process, so your app's queries do not wait on the network. Your app keeps its
SQLite engine and driver. [How backups work](docs/sqlite-backup.md)

**Where does my data live?**\
Your app's SQLite files stay on its own disk. Backups live on a Litebackup
server you run and in buckets you own, encrypted before they leave the server.

**What if I lose the server?**\
Start a new server and recover it from your bucket with the recovery kit or
passphrase you saved at setup. [Server recovery](docs/sqlite-restore.md#recover-after-losing-the-server)

**Do I have to change my code?**\
One SDK call registers a database, or `litebackup start data/main.sqlite`
backs it up with no code change at all.

**What does it cost?**\
Plans are priced by the number of databases and machines, and there is a
free plan. See [pricing](https://litebackup.com/pricing).


## Getting started

### 1. Install your server

You run the server on your own VM, and claiming it gives it an address under
litebackup.cloud with a TLS certificate. On a Debian 13 or Ubuntu 26.04 LTS
VM with a public IP and TCP 443 open (2 vCPUs and 2 to 4 GB of RAM are enough
to start), run:

```sh
curl -fsSL https://litebackup.com/server.sh | bash
```

Open the link it prints, claim your instance, add a bucket, and save your
recovery kit somewhere other than the VM. Then open **Machines → Add
machine** and copy the join token.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/console-dark.png">
  <img src="docs/assets/console.png" alt="Litebackup console showing database backup status, versions, and the machine writing each database">
</picture>

<sub>[Server setup and sizing](docs/litebackup-server.md) · [Bucket providers](docs/sqlite-cloud-backup.md) · <a href="https://cloud.digitalocean.com/droplets/new?image=debian-13-x64&amp;refcode=13cb93d1ec16">Create a Debian 13 Droplet on DigitalOcean</a> (referral link)</sub>

### 2. Install the SDK

| Language | Install |
| --- | --- |
| TypeScript (Node.js 22+) | `npm install @litebackup/sdk better-sqlite3` |
| Python 3.11+ | `pip install litebackup` |
| Go | `go get github.com/litebackup/sdk/litebackup` |

Then give the machine its credential, the join token followed by `@` and your
server's address:

```sh
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
```

The npm package and the Python wheels bundle the Litebackup client, which
starts and joins your server on first use. Go apps install it with
`curl -fsSL https://litebackup.com/cli.sh | bash`.

### 3. Back up a database

Register the file your app opens. The call returns once the backup rule is
saved, and the client backs up every later change in the background. Calling
it again on the next start returns the same rule.

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

<details>
<summary>Python</summary>

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
</details>

<details>
<summary>Go</summary>

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

Add the driver with `go get modernc.org/sqlite`.
</details>

<details>
<summary>Command line, no code change</summary>

```sh
litebackup start data/main.sqlite
```
</details>

### 4. Restore it

Restore the newest version, or the database as it was at any time your
retention keeps, into a new file on this or any joined machine:

```ts
import Database from "better-sqlite3";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.databases.restore("main.sqlite", { path: "data/restored.sqlite" });
console.log(new Database("data/restored.sqlite").prepare("SELECT body FROM notes").all());
```

<details>
<summary>Python and command line</summary>

```python
import sqlite3

from litebackup import Litebackup

with Litebackup() as litebackup:
    litebackup.databases.restore("main.sqlite", path="data/restored.sqlite")
print(sqlite3.connect("data/restored.sqlite").execute("SELECT body FROM notes").fetchall())
```

```sh
litebackup restore main.sqlite "${PWD}/data/restored.sqlite"
litebackup restore main.sqlite "${PWD}/data/before.sqlite" --time 2026-11-15T09:30:00Z
```
</details>

A restore writes a new file and never overwrites an existing one. To roll
back under the same name, `litebackup revert main.sqlite --version 2`
publishes that version as the newest. Every later version stays restorable.
[Point-in-time restore](docs/sqlite-restore.md)

## What you can build

| Use case | Guide |
| --- | --- |
| A database for every tenant, backed up with one glob | [Multitenancy](docs/sqlite-multitenancy.md) |
| Agent or customer state that moves between machines | [Agent state](docs/sqlite-agent-state.md) |
| Live readonly replicas on other machines | [Replication](docs/sqlite-replication.md) |
| An app that keeps writing when a server goes down | [High availability](https://litebackup.com/failover) |
| Backups from cron, CI, or a serverless function | [One-shot jobs](docs/sqlite-backup-script.md) |
| better-sqlite3, node:sqlite, Drizzle, sqlite3, aiosqlite, SQLAlchemy, Django, FastAPI, or Go | [Integrations](docs/sqlite-integrations.md) |
| The client as a service or in Docker | [Client runtime](docs/litebackup-client.md), [Docker](docs/sqlite-docker.md) |

## More questions

<details>
<summary><strong>When should my code wait for a backup?</strong></summary>

Usually never. `backups.start` saves the rule and returns, and the client
uploads in the background. Foreground jobs (`backups.once`,
`litebackup backup`) and releasing a database you acquired wait for your
server by default. Your server archives each version to your bucket after
its archive delay, so wait for `cloud` only when a job's machine goes away
afterwards and holds the only copy.
</details>

<details>
<summary><strong>What is LITEBACKUP_API_KEY?</strong></summary>

The one credential a machine needs: a join token or an API key, then `@`
and your server's address. A join token is exchanged once for the machine's
own key; an API key is used as it is.
[Client credentials](docs/litebackup-client.md#join-and-register)
</details>

## Documentation

| Guide | Covers |
| --- | --- |
| [API reference](docs/sdk-reference.md) | TypeScript, Python, and Go operations, options, errors, and status |
| [Live SQLite backup](docs/sqlite-live-backup.md) | Backing up a running database and its WAL |
| [Recover deleted rows](docs/sqlite-deleted-rows.md) | Restoring selected records without replacing the database |
| [Storage providers](docs/sqlite-cloud-backup.md) | S3, R2, Google Cloud Storage, and S3-compatible buckets |
| [Troubleshooting](docs/sqlite-backup-errors.md) | Join failures, name conflicts, timeouts, and diagnostics |
| [Changelog](CHANGELOG.md) | What each release changes |

## License

The SDKs are released under the [MIT License](LICENSE).
