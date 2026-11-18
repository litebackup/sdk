import assert from "node:assert/strict";
import { execFile, execFileSync, spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, openSync } from "node:fs";
import { mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { test, type TestContext } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { promisify } from "node:util";

import { Litebackup, LitebackupError, sessionSocket, backup, restore, versions } from "../src/index.js";

const run = promisify(execFile);
const binary = process.env.LITEBACKUP_PATH;
const skip = process.env.LITEBACKUP_INTEGRATION === "1" && binary ? false : "set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH to run against a development server";

/** Returns a loopback port nothing listens on. */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const listener = createServer();
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", () => {
      const address = listener.address();
      listener.close(() => resolve(typeof address === "object" && address ? address.port : 0));
    });
  });
}

/** Adds rows to a SQLite file with Node's built-in driver, creating it when needed. */
function addRows(path: string, rows: number): void {
  mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);
  db.exec("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body BLOB)");
  const insert = db.prepare("INSERT INTO items (body) VALUES (randomblob(2000))");
  for (let i = 0; i < rows; i++) {
    insert.run();
  }
  db.close();
}

/** Counts the rows of a database. */
function countRows(path: string): number {
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    return Number((db.prepare("SELECT count(*) AS n FROM items").get() as { n: number }).n);
  } finally {
    db.close();
  }
}

/** Calls check until it stops throwing or the timeout passes. */
async function eventually<T>(timeoutMillis: number, check: () => Promise<T>): Promise<T> {
  const deadline = Date.now() + timeoutMillis;
  for (;;) {
    try {
      return await check();
    } catch (error) {
      if (Date.now() > deadline) {
        throw error;
      }
      await delay(250);
    }
  }
}

/** A `litebackup server run --development` with a local bucket and the sessions a test joined. */
class DevelopmentServer {
  readonly clients: Litebackup[] = [];
  private constructor(
    readonly run: string,
    readonly directory: string,
    readonly data: string,
    readonly address: string,
    private readonly process: ChildProcess,
  ) {}

  /** Runs the server, adds a local bucket, and completes setup, as the litebackup repository's tests do. */
  static async start(t: TestContext): Promise<DevelopmentServer> {
    const directory = await mkdtemp(join(tmpdir(), "lb"));
    // The run token names the data directory and every session, so the
    // watchdog finds this run's processes and no one else's.
    const runToken = basename(directory).toLowerCase();
    // A detached shell kills every process of this run once the test process exits, however it exits.
    spawn("sh", ["-c", `while kill -0 ${process.pid} 2>/dev/null; do sleep 1; done; pkill -9 -f ${runToken}`], { detached: true, stdio: "ignore" }).unref();
    const data = join(directory, "server");
    const address = `127.0.0.1:${await freePort()}`;
    const log = openSync(join(directory, "server.log"), "a");
    const child = spawn(binary!, ["server", "run", "--development", "--data-directory", data, "--listen-address", address, "--archive-delay", "1s", "--minimum-sync-interval", "0s", "--session-lease", "3s", "--session-heartbeat", "1s"], { stdio: ["ignore", log, log] });
    const server = new DevelopmentServer(runToken, directory, data, address, child);
    const saved = { ...process.env };
    // Every daemon this test starts keeps its sockets and state here.
    process.env.XDG_RUNTIME_DIR = join(directory, "run");
    process.env.XDG_STATE_HOME = join(directory, "state");
    delete process.env.LITEBACKUP_TOKEN;
    delete process.env.LITEBACKUP_SESSION;
    t.after(async () => {
      // Stop the daemons before the server, because their shutdown ends their sessions on it.
      for (const client of server.clients) {
        client.close();
        await client.shutdown({ wait: "none" }).catch(() => undefined);
      }
      spawnSync("pkill", ["-9", "-f", `(daemon --session .*-${runToken}$)|(${runToken}/server)`]);
      child.kill("SIGKILL");
      process.env = saved;
      await rm(directory, { recursive: true, force: true });
    });
    await eventually(30_000, () => server.joinToken("probe"));
    await server.admin("bucket", "create", "--provider", "s3", "--endpoint", `file://${join(directory, "bucket")}`, "--bucket", "local", "--name", "local");
    await server.admin("setup", "complete", "key_backup");
    await server.admin("setup", "complete", "retention");
    return server;
  }

  /** Runs a `litebackup server` command against the data directory. */
  admin(...arguments_: string[]): Promise<{ stdout: string }> {
    return run(binary!, ["server", ...arguments_, "--data-directory", this.data]);
  }

  /** Creates a join token named after a session. */
  async joinToken(name: string): Promise<string> {
    const created = await this.admin("join-token", "create", "--name", name, "--stdout-format", "json");
    return `${(JSON.parse(created.stdout) as { token: string }).token}@http://${this.address}`;
  }

  /** Starts a session's daemon with its own join token, as an app on another machine would. */
  async connect(name: string): Promise<Litebackup> {
    const session = this.session(name);
    const client = await Litebackup.connect({ token: await this.joinToken(session), session });
    this.clients.push(client);
    return client;
  }

  /** Starts a session's daemon again; it keeps the join it made. */
  async reconnect(name: string): Promise<Litebackup> {
    const client = await Litebackup.connect({ session: this.session(name) });
    this.clients.push(client);
    return client;
  }

  /** Stops a session's daemon with SIGKILL, so its lease runs out on the server. */
  kill(name: string): void {
    const output = execFileSync("pgrep", ["-f", `daemon --session ${this.session(name)}$`], { encoding: "utf8" });
    for (const pid of output.split(/\s+/).filter(Boolean)) {
      process.kill(Number(pid), "SIGKILL");
    }
  }

  /** Names a session of this run. */
  session(name: string): string {
    return `${name}-${this.run}`;
  }

  path(...elements: string[]): string {
    const path = join(this.directory, ...elements);
    mkdirSync(dirname(path), { recursive: true });
    return path;
  }
}

test("backs up, names, replicates, restores, acquires, and releases", { skip, timeout: 300_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const web1 = await server.connect("web-1");
  const web2 = await server.connect("web-2");
  const reader = await server.connect("reader");
  assert.ok(web1.clientVersion, "the daemon did not report its version");

  // Backups and name collisions.
  const mainPath = server.path("web-1", "main.db");
  addRows(mainPath, 10);
  const main = await web1.startBackup(mainPath);
  assert.equal(main.name, "main.db");
  await web1.sync(main.name);
  const otherPath = server.path("web-2", "main.db");
  addRows(otherPath, 3);
  await assert.rejects(web2.backup(otherPath), (error: unknown) => error instanceof LitebackupError && error.code === "busy");
  const prefixed = await web2.startBackup(otherPath, { prefix: "web-2/" });
  await web2.sync(prefixed.name);
  assert.equal(prefixed.name, "web-2/main.db");
  assert.deepEqual((await web2.backups.list()).map((listed) => listed.id), [prefixed.id]);

  // Versions and restore.
  addRows(mainPath, 5);
  await web1.sync(main.name);
  const listed = await web1.versions("main.db");
  assert.ok(listed.length >= 2, `versions: ${JSON.stringify(listed)}`);
  const restoredPath = server.path("restored", "main.db");
  const restored = await web2.restore("main.db", { to: restoredPath, version: listed[0]!.version });
  assert.equal(restored.version, listed[0]!.version);
  assert.equal(countRows(restoredPath), 10);
  await assert.rejects(web2.restore("main.db", { to: restoredPath }), (error: unknown) => error instanceof LitebackupError && error.code === "exists");

  // The package helpers share a client of LITEBACKUP_SESSION.
  process.env.LITEBACKUP_SESSION = server.session("web-2");
  assert.equal((await versions("main.db")).length, listed.length);
  const headPath = server.path("restored", "head.db");
  await restore("main.db", { to: headPath, time: new Date() });
  assert.equal(countRows(headPath), 15);
  const helperPath = server.path("web-2", "helper.db");
  addRows(helperPath, 1);
  await backup(helperPath, { prefix: "web-2/" });

  // A replica on a readonly machine.
  const replicaPath = server.path("reader", "main.db");
  const replica = await reader.replicate("main.db", { to: replicaPath });
  assert.equal(replica.role, "replica");
  assert.equal(replica.syncedServer.version, (await web1.database("main.db")).syncedServer.version);
  assert.equal(countRows(replicaPath), 15);
  const writer = new DatabaseSync(replicaPath, { timeout: 100 });
  assert.throws(() => writer.exec("DELETE FROM items"), (error: unknown) => (error as { errcode?: number }).errcode === 5, "app writes to the replica get SQLITE_BUSY");
  writer.close();
  assert.equal((await reader.replicas.list()).replicas.length, 1);

  // Acquire and release move a database between machines.
  const tenantPath = server.path("web-1", "42.db");
  addRows(tenantPath, 4);
  const acquired = await web1.acquire("tenants/42.db", { to: tenantPath, maintenance: true });
  assert.equal(acquired.outcome, "created");
  assert.equal(acquired.database.acquired, true);
  const holder = await web1.writer("tenants/42.db");
  assert.equal(holder.thisMachine, true);
  assert.equal(holder.active, true);
  addRows(tenantPath, 2);
  const released = await web1.release("tenants/42.db", { wait: "server" });
  assert.ok(released.database.syncedServer.version >= 2);
  const movedPath = server.path("web-2", "42.db");
  const moved = await web2.acquire("tenants/42.db", { to: movedPath });
  assert.equal(moved.outcome, "downloaded");
  assert.equal(countRows(movedPath), 6);
  await web2.release("tenants/42.db", { wait: "server" });
  assert.deepEqual(await web2.conflicts.list(), []);
});

// The test server's session lease is 3 seconds, so a replica takes over a
// dead origin within seconds. The origin dies and exactly one replica takes
// over; the old origin comes back with its writes saved as a conflict; then the new
// origin dies too and the last replica takes over.
test("fails over between three machines and saves the old origin's writes as a conflict", { skip, timeout: 300_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const first = await server.connect("first");
  const second = await server.connect("second");
  const third = await server.connect("third");
  const name = "shared.db";

  const firstPath = server.path("first", "shared.db");
  addRows(firstPath, 5);
  const firstFailover = await first.failover(firstPath, { name });
  assert.equal(firstFailover.isOrigin, true);
  await first.sync(name);
  const secondFailover = await second.failover(server.path("second", "shared.db"), { name });
  const thirdPath = server.path("third", "shared.db");
  const thirdFailover = await third.failover(thirdPath, { name });
  const tookOver = new Promise<string>((resolve) => {
    secondFailover.on("origin", () => resolve("second"));
    thirdFailover.on("origin", () => resolve("third"));
  });
  assert.equal(secondFailover.role, "replica");
  assert.equal(thirdFailover.role, "replica");
  await eventually(30_000, async () => assert.equal(countRows(thirdPath), 5));

  // The origin dies; one replica takes over about a lease later.
  server.kill("first");
  const killed = Date.now();
  const winner = await Promise.race([tookOver, delay(30_000, "timeout" as const)]);
  assert.notEqual(winner, "timeout");
  assert.ok(Date.now() - killed < 6_000, `the takeover took ${Date.now() - killed} ms with a 3 s lease`);
  await delay(2_000);
  assert.notEqual(secondFailover.isOrigin, thirdFailover.isOrigin, "exactly one origin");
  const [origin, replica] = winner === "second" ? [secondFailover, thirdFailover] : [thirdFailover, secondFailover];
  // The old origin's app kept writing while its daemon was gone.
  addRows(firstPath, 3);

  // The first machine comes back: its writes are saved as a conflict and it follows the new origin.
  const restarted = await server.reconnect("first");
  const comeback = await restarted.failover(firstPath, { name });
  // The restarted daemon resumes the failover rule on its own and may set
  // the writes aside before this failover call; its event log has them.
  await eventually(60_000, async () => {
    const page = await restarted.eventPage({ limit: 10_000 });
    const saved = page.events.find((event) => event.type === "conflict_saved" && event.name === name);
    assert.ok(saved?.path, "no conflict_saved event with a path yet");
    assert.ok(existsSync(saved.path), `${saved.path} is missing`);
  });
  await eventually(60_000, async () => assert.equal(comeback.role, "replica"));
  assert.equal(countRows(firstPath), 5);
  assert.equal(origin.isOrigin, true);

  // The new origin dies too; the last replica takes over.
  server.kill(winner);
  await eventually(30_000, async () => assert.ok(replica.isOrigin || comeback.isOrigin, "no replica took over yet"));
  await delay(2_000);
  assert.notEqual(replica.isOrigin, comeback.isOrigin, "exactly one origin");
  await replica.stop();
});

// An app that opens a new database and closes it without writing leaves a
// 0-byte file. Backup and acquire treat it as a new, empty database.
test("backs up and acquires empty files as new databases", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const web1 = await server.connect("web-1");
  const web2 = await server.connect("web-2");

  const emptyPath = server.path("web-1", "empty.db");
  new DatabaseSync(emptyPath).close();
  const empty = await web1.startBackup(emptyPath);
  assert.equal((await web1.database("empty.db")).syncedServer.version, 0);
  addRows(emptyPath, 1);
  await web1.sync(empty.name);
  assert.equal((await web1.database("empty.db")).syncedServer.version, 1);

  const tenantPath = server.path("web-1", "7.db");
  new DatabaseSync(tenantPath).close();
  const created = await web1.acquire("tenants/7.db", { to: tenantPath });
  assert.equal(created.outcome, "created");
  addRows(tenantPath, 3);
  await web1.release("tenants/7.db", { wait: "server" });
  const movedPath = server.path("web-2", "7.db");
  new DatabaseSync(movedPath).close();
  const moved = await web2.acquire("tenants/7.db", { to: movedPath });
  assert.equal(moved.outcome, "downloaded");
  assert.equal(countRows(movedPath), 3);
  assert.deepEqual(await web2.conflicts.list(), []);
});

test("prefix replicas download existing and future databases and stop as a group", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const origin = await server.connect("prefix-origin");
  const reader = await server.connect("prefix-reader");
  const firstPath = server.path("prefix-origin", "first.db");
  addRows(firstPath, 2);
  await origin.backup(firstPath, { prefix: "tenants/" });
  const directory = server.path("prefix-reader", "replicas");
  mkdirSync(directory, { recursive: true });
  const prefix = await reader.replicatePrefix("tenants", { to: directory });
  assert.equal(prefix.prefix, "tenants");
  const firstCopy = join(directory, "first.db");
  await eventually(30_000, async () => {
    assert.ok(existsSync(firstCopy));
    assert.equal(countRows(firstCopy), 2);
  });
  const secondPath = server.path("prefix-origin", "second.db");
  addRows(secondPath, 3);
  await origin.backup(secondPath, { prefix: "tenants/" });
  const secondCopy = join(directory, "second.db");
  await eventually(30_000, async () => {
    assert.ok(existsSync(secondCopy));
    assert.equal(countRows(secondCopy), 3);
  });
  await reader.replicas.stopPrefix("tenants", directory);
  assert.equal((await reader.replicas.list()).prefixes.length, 0);
  const writable = new DatabaseSync(firstCopy);
  writable.exec("INSERT INTO items (body) VALUES ('local after stop')");
  writable.close();
  assert.equal(countRows(firstCopy), 3);
});

test("documented one-shot runners archive their files and leave no backup rules", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const job = await server.connect("job");
  const environment = { ...process.env, LITEBACKUP_SESSION: server.session("job") };
  const jsPath = server.path("job", "js-input.db");
  addRows(jsPath, 4);
  await run(process.execPath, [new URL("../examples/one-shot.js", import.meta.url).pathname, jsPath], { env: environment });
  assert.deepEqual(await job.backups.list(), []);
  const jsRestored = server.path("restored", "js-input.db");
  await job.restore("scheduled/js-input.db", { to: jsRestored });
  assert.equal(countRows(jsRestored), 4);

  const binDirectory = server.path("bin");
  mkdirSync(binDirectory, { recursive: true });
  await symlink(binary!, join(binDirectory, "litebackup"));
  const cliPath = server.path("job", "cli-input.db");
  addRows(cliPath, 5);
  await run("bash", [new URL("../../../examples/cron/backup-sqlite.sh", import.meta.url).pathname, cliPath], {
    env: { ...environment, PATH: `${binDirectory}:${process.env.PATH}` },
  });
  assert.deepEqual(await job.backups.list(), []);
  const cliRestored = server.path("restored", "cli-input.db");
  await job.restore("scheduled/cli-input.db", { to: cliRestored });
  assert.equal(countRows(cliRestored), 5);

  // A one-shot job must not stop a continuous backup of the same file.
  const continuousPath = server.path("job", "continuous.db");
  addRows(continuousPath, 2);
  const continuous = await job.startBackup(continuousPath);
  await job.sync(continuous.name);
  await assert.rejects(
    run("bash", [new URL("../../../examples/cron/backup-sqlite.sh", import.meta.url).pathname, continuousPath], {
      env: { ...environment, PATH: `${binDirectory}:${process.env.PATH}` },
    }),
    (error: unknown) => {
      const failure = error as { stderr: string };
      return failure.stderr.includes("busy") && !failure.stderr.includes("Could not stop backup");
    },
  );
  await assert.rejects(run(process.execPath, [new URL("../examples/one-shot.js", import.meta.url).pathname, continuousPath], { env: environment }));
  assert.deepEqual((await job.backups.list()).map((backup) => backup.id), [continuous.id]);
  await job.sync(continuous.name);
  assert.deepEqual((await job.backups.list()).map((backup) => backup.id), [continuous.id]);
  await job.stopBackup(continuous.name);
});

test("foreground recursive backup reports bad files and preserves relative names", { skip, timeout: 30_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const reader = await server.connect("reader");
  const session = server.session("scheduled");
  const token = await server.joinToken(session);
  const root = server.path("source");
  addRows(join(root, "Équipe", "42.sqlite"), 2);
  addRows(join(root, "Main.sqlite"), 3);
  const rootLink = server.path("source-link");
  await symlink(root, rootLink);
  await symlink(root, join(root, "cycle"));
  await writeFile(join(root, "invalid.sqlite"), "invalid");
  await assert.rejects(
    backup(join(rootLink, "**", "*.sqlite"), { session, token, prefix: "租户/" }),
    (err: unknown) => err instanceof LitebackupError && err.message.includes("invalid.sqlite"),
  );
  for (const name of ["租户/Équipe/42.sqlite", "租户/Main.sqlite"]) {
    assert.equal((await reader.versions(name)).length, 1);
  }
  const source = join(root, "Main.sqlite");
  const exact = "Mixed Case/É.sqlite";
  await backup(source, { session, token, name: exact, wait: "cloud" });
  const restored = server.path("result", "Exact.sqlite");
  await reader.restore(exact, { to: restored });
  assert.equal(countRows(restored), 3);
  const controller = new AbortController();
  const pending = backup(source, { session, name: "cancel.sqlite", wait: "cloud", signal: controller.signal });
  const cancelled = assert.rejects(pending, (err: unknown) => err instanceof LitebackupError && err.code === "cancelled");
  await eventually(3_000, async () => {
    assert.ok((await reader.writer("cancel.sqlite")).active);
  });
  controller.abort();
  await cancelled;
  assert.equal((await reader.writer("cancel.sqlite")).active, false);
  assert.equal(existsSync(sessionSocket(session, process.env).socketPath), false);
});

test("stop selects single files by remote name and whole backup rules by ID", { skip, timeout: 30_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const client = await server.connect("rules");
  const env = { ...process.env, LITEBACKUP_SESSION: server.session("rules") };
  const cli = (...args: string[]) => run(binary!, args, { cwd: server.directory, env });
  const path = server.path("source", "local.sqlite");
  addRows(path, 2);
  const single = await client.startBackup(path, { prefix: "Équipe/" });
  assert.equal(single.name, "Équipe/local.sqlite");
  await client.sync(single.name);
  await rm(path);
  assert.equal((await client.backups.list())[0]?.name, single.name);
  await assert.rejects(cli("stop", path));
  await cli("stop", single.name);
  assert.ok((await client.versions(single.name)).length > 0);

  addRows(path, 1);
  const siblingPath = server.path("source", "other.sqlite");
  addRows(siblingPath, 1);
  const group = await client.startBackup(join(dirname(path), "**", "*.sqlite"), { prefix: "Équipe/" });
  const empty = await client.startBackup(join(dirname(path), "*.empty"), { prefix: "Équipe/" });
  await eventually(5_000, async () => {
    assert.deepEqual((await client.databases()).map((db) => db.name), [single.name, "Équipe/other.sqlite"]);
  });
  await assert.rejects(client.stopBackup(single.name), (err: unknown) =>
    err instanceof LitebackupError && err.code === "invalid_argument" && err.message.includes(group.id));
  await assert.rejects(cli("stop", "--rule", group.id, single.name));
  assert.equal((await client.backups.list()).length, 2);
  assert.equal((await client.databases()).length, 2);
  await cli("stop", "--rule", group.id);
  assert.deepEqual((await client.backups.list()).map((rule) => rule.id), [empty.id]);
  assert.deepEqual(await client.databases(), []);
  await client.stopBackupRule(empty.id);
  assert.deepEqual(await client.backups.list(), []);
});
