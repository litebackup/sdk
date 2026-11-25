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

import { ErrorCode, LeaseCleanupError, Litebackup, LitebackupError, sessionSocket, type Adapter } from "../src/index.js";

const executeFile = promisify(execFile);
const binary = process.env.LITEBACKUP_PATH;
const skip = process.env.LITEBACKUP_INTEGRATION === "1" && binary ? false : "set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH";

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
function addItems(path: string, itemCount: number): void {
  mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);
  db.exec("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body BLOB)");
  const insert = db.prepare("INSERT INTO items (body) VALUES (randomblob(2000))");
  for (let i = 0; i < itemCount; i++) {
    insert.run();
  }
  db.close();
}

/** Counts the itemCount of a database. */
function countItems(path: string): number {
  const db = new DatabaseSync(path, { readOnly: true }); // vocabulary:allow node:sqlite names the option
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
    readonly tag: string,
    readonly directory: string,
    readonly data: string,
    readonly address: string,
    private readonly process: ChildProcess,
  ) {}

  /** Runs the server, adds a local bucket, and completes setup. */
  static async start(t: TestContext): Promise<DevelopmentServer> {
    const directory = await mkdtemp(join(tmpdir(), "lb"));
    // The tag names the data directory and every session, so the
    // watchdog finds this run's processes and no one else's.
    const tag = basename(directory).toLowerCase();
    // A detached shell kills every process of this run once the test process exits, however it exits.
    spawn("sh", ["-c", `while kill -0 ${process.pid} 2>/dev/null; do sleep 1; done; pkill -9 -f ${tag}`], { detached: true, stdio: "ignore" }).unref();
    const data = join(directory, "server");
    const address = `127.0.0.1:${await freePort()}`;
    const log = openSync(join(directory, "server.log"), "a");
    const child = spawn(binary!, ["server", "run", "--development", "--data-directory", data, "--listen-address", address, "--archive-delay", "1s", "--minimum-sync-interval", "0s", "--session-lease", "3s", "--session-heartbeat", "1s"], { stdio: ["ignore", log, log] });
    const server = new DevelopmentServer(tag, directory, data, address, child);
    const saved = { ...process.env };
    // Every daemon this test starts keeps its sockets and state here.
    process.env.XDG_RUNTIME_DIR = join(directory, "run");
    process.env.XDG_STATE_HOME = join(directory, "state");
    delete process.env.LITEBACKUP_API_KEY;
    delete process.env.LITEBACKUP_SESSION;
    t.after(async () => {
      // Stop the daemons before the server, because their shutdown ends their sessions on it.
      for (const client of server.clients) {
        await client.session.shutdown({ durability: "none" }).catch(() => undefined);
        client.close();
      }
      spawnSync("pkill", ["-9", "-f", `(daemon --session .*-${tag}$)|(${tag}/server)`]);
      child.kill("SIGKILL");
      process.env = saved;
      await rm(directory, { recursive: true, force: true });
    });
    await eventually(30_000, () => server.joinToken("probe"));
    await server.admin("bucket", "create", "--provider", "s3", "--endpoint", `file://${join(directory, "bucket")}`, "--bucket", "bucket", "--name", "local");
    await server.admin("setup", "complete", "key_backup");
    await server.admin("setup", "complete", "retention");
    return server;
  }

  /** Runs a `litebackup server` command against the data directory. */
  admin(...arguments_: string[]): Promise<{ stdout: string }> {
    return executeFile(binary!, ["server", ...arguments_, "--data-directory", this.data]);
  }

  /** Creates a join token named after a session. */
  async joinToken(name: string): Promise<string> {
    const created = await this.admin("join-token", "create", "--name", name, "--stdout-format", "json");
    return `${(JSON.parse(created.stdout) as { token: string }).token}@http://${this.address}`;
  }

  /** Starts a session's daemon with its own join token, as an app on another machine would. */
  async connect(name: string): Promise<Litebackup> {
    const session = this.session(name);
    const client = new Litebackup({ apiKey: await this.joinToken(session), session });
    await client.connect();
    this.clients.push(client);
    return client;
  }

  /** Starts a session's daemon again; it keeps the join it made. */
  async reconnect(name: string): Promise<Litebackup> {
    const client = new Litebackup({ session: this.session(name) });
    await client.connect();
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
    return `${name}-${this.tag}`;
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
  addItems(mainPath, 10);
  const main = await web1.backups.start(mainPath);
  assert.equal(main.name, "main.db");
  await web1.databases.sync(main.name);
  const web2Path = server.path("web-2", "main.db");
  addItems(web2Path, 3);
  await assert.rejects(web2.backups.once(web2Path), (error: unknown) => error instanceof LitebackupError && error.code === "busy");
  const prefixed = await web2.backups.start(web2Path, { prefix: "web-2/" });
  await web2.databases.sync(prefixed.name);
  assert.equal(prefixed.name, "web-2/main.db");
  assert.deepEqual((await web2.rules.list()).map((listed) => listed.id), [prefixed.id]);

  // Versions and restore.
  addItems(mainPath, 5);
  await web1.databases.sync(main.name);
  const listed = await web1.databases.versions("main.db");
  assert.ok(listed.length >= 2, `versions: ${JSON.stringify(listed)}`);
  const restoredPath = server.path("restored", "main.db");
  const restored = await web2.databases.restore("main.db", { path: restoredPath, version: listed[0]!.version });
  assert.equal(restored.version, listed[0]!.version);
  assert.equal(countItems(restoredPath), 10);
  await assert.rejects(web2.databases.restore("main.db", { path: restoredPath }), (error: unknown) => error instanceof LitebackupError && error.code === ErrorCode.fileExists);

  // Select a previously joined session through the environment.
  process.env.LITEBACKUP_SESSION = server.session("web-2");
  const fromEnvironment = new Litebackup();
  assert.equal((await fromEnvironment.databases.versions("main.db")).length, listed.length);
  const headPath = server.path("restored", "head.db");
  await fromEnvironment.databases.restore("main.db", { path: headPath, time: new Date() });
  assert.equal(countItems(headPath), 15);
  const helperPath = server.path("web-2", "helper.db");
  addItems(helperPath, 1);
  await fromEnvironment.backups.once(helperPath, { prefix: "web-2/" });

  // A replica on a readonly machine.
  const replicaPath = server.path("reader", "main.db");
  const replica = await reader.replicas.start("main.db", { path: replicaPath });
  assert.equal(replica.role, "replica");
  assert.equal(replica.syncedServer.version, (await web1.databases.get("main.db")).syncedServer.version);
  assert.equal(countItems(replicaPath), 15);
  const writer = new DatabaseSync(replicaPath, { timeout: 100 });
  assert.throws(() => writer.exec("DELETE FROM items"), (error: unknown) => (error as { errcode?: number }).errcode === 5, "app writes to the replica get SQLITE_BUSY");
  writer.close();
  assert.equal((await reader.replicas.list()).replicas.length, 1);

  // Acquire and release move a database between machines.
  const tenantPath = server.path("web-1", "42.db");
  addItems(tenantPath, 4);
  const acquired = await web1.databases.acquire("tenants/42.db", { path: tenantPath, maintenance: true });
  assert.equal(acquired.decision, "created");
  assert.equal(acquired.database.acquired, true);
  const tenantWriter = await web1.databases.writer("tenants/42.db");
  assert.equal(tenantWriter.thisMachine, true);
  assert.equal(tenantWriter.active, true);
  addItems(tenantPath, 2);
  const released = await web1.databases.release("tenants/42.db", { durability: "server" });
  assert.ok(released.database.syncedServer.version >= 2);
  const movedPath = server.path("web-2", "42.db");
  const moved = await web2.databases.acquire("tenants/42.db", { path: movedPath });
  assert.equal(moved.decision, "downloaded");
  assert.equal(countItems(movedPath), 6);
  await web2.databases.release("tenants/42.db", { durability: "server" });
});

// The test server's session lease is 3 seconds, so a replica takes over a
// dead origin within seconds. The origin dies and exactly one replica takes
// over; the old origin comes back with writes the server lacks and is refused,
// its file left as it is; then the new origin dies too and the last replica
// takes over.
test("fails over between three machines and refuses the old origin's diverged file", { skip, timeout: 300_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const first = await server.connect("first");
  const second = await server.connect("second");
  const third = await server.connect("third");
  const name = "shared.db";

  const firstPath = server.path("first", "shared.db");
  addItems(firstPath, 5);
  const firstFailover = await first.failovers.start(firstPath, { name });
  assert.equal(firstFailover.isOrigin, true);
  await first.databases.sync(name);
  const secondFailover = await second.failovers.start(server.path("second", "shared.db"), { name });
  const thirdPath = server.path("third", "shared.db");
  const thirdFailover = await third.failovers.start(thirdPath, { name });
  const promoted = new Promise<string>((resolve) => {
    secondFailover.on("origin", () => resolve("second"));
    thirdFailover.on("origin", () => resolve("third"));
  });
  assert.equal(secondFailover.role, "replica");
  assert.equal(thirdFailover.role, "replica");
  await eventually(30_000, async () => assert.equal(countItems(thirdPath), 5));

  // The origin dies; one replica takes over about a lease later.
  server.kill("first");
  const killed = Date.now();
  // The old origin's follower loses its daemon: its role is unknown at once, so a health route stops sending it writes.
  await eventually(2_000, async () => assert.equal(firstFailover.role, "unknown"));
  assert.equal(firstFailover.isOrigin, false);
  const winner = await Promise.race([promoted, delay(30_000, "timeout" as const)]);
  assert.notEqual(winner, "timeout");
  assert.ok(Date.now() - killed < 6_000, `the takeover took ${Date.now() - killed} ms with a 3 s lease`);
  await delay(2_000);
  assert.notEqual(secondFailover.isOrigin, thirdFailover.isOrigin, "exactly one origin");
  const [origin, replica] = winner === "second" ? [secondFailover, thirdFailover] : [thirdFailover, secondFailover];
  // The old origin's app kept writing while its daemon was gone.
  addItems(firstPath, 3);

  // The first machine comes back: its file holds writes the new origin never
  // saw, so it is refused and left as it is. The restarted daemon resumes the
  // failover rule on its own and may refuse the file before this call, which
  // then answers conflict.
  const restarted = await server.reconnect("first");
  await restarted.failovers.start(firstPath, { name }).catch((error: unknown) => {
    if (!(error instanceof LitebackupError) || error.code !== "conflict") throw error;
  });
  await eventually(60_000, async () => {
    const page = await restarted.events.page({ limit: 10_000 });
    const denied = page.events.find((event) => event.type === "denied" && event.name === name && event.path === firstPath);
    assert.ok(denied, "no denied event naming the file yet");
  });
  assert.equal(countItems(firstPath), 8);
  assert.equal(origin.isOrigin, true);

  // The new origin dies too; the last replica takes over.
  server.kill(winner);
  await eventually(30_000, async () => assert.ok(replica.isOrigin, "no replica took over yet"));
  await replica.stop();
});

// An app that opens a new database and closes it without writing leaves a
// 0-byte file. Backup and acquire treat it as a new, empty database.
/** An adapter whose handle is the path itself, so the work writes through its own connections; close fails with failClose. */
function pathAdapter(failClose?: Error): Adapter<string> {
  return {
    open: (path) => path,
    close: () => {
      if (failClose) throw failClose;
    },
  };
}

/** Keeps a database open in a child process until the test ends, so a release fails with still_open. */
async function keepOpen(t: TestContext, path: string): Promise<void> {
  const script = "const { DatabaseSync } = require('node:sqlite'); const db = new DatabaseSync(process.argv[1]); db.prepare('SELECT count(*) FROM sqlite_schema').get(); console.log('open'); setTimeout(() => {}, 60000);";
  const app = spawn(process.execPath, ["--eval", script, path], { stdio: ["ignore", "pipe", "inherit"] });
  t.after(() => {
    app.kill("SIGKILL");
  });
  await new Promise<void>((resolve, reject) => {
    app.stdout!.once("data", () => resolve());
    app.once("exit", (code) => reject(new Error(`the app exited with ${code} before it opened ${path}`)));
  });
}

test("a pool leases a database between calls and releases it", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const worker = await server.connect("worker");

  // A lease with cloud durability resolves once the bucket holds its work, keeps the lock, and offloads when the pool releases it.
  const agentPath = server.path("worker", "agents", "42.sqlite");
  let pool = worker.databases.pool({ adapter: pathAdapter() });
  await pool.lease("agents/42.sqlite", { path: agentPath, durability: "cloud", offload: true }, (path) => addItems(path, 5));
  let status = await worker.databases.get("agents/42.sqlite");
  assert.ok(status.syncedCloud.version > 0 && status.syncedCloud.version === status.syncedServer.version && status.acquired, JSON.stringify(status));
  await pool.release("agents/42.sqlite");
  assert.equal((await worker.databases.get("agents/42.sqlite")).state, "offloaded");

  // Failed work keeps the lease and its error; the release then does not offload.
  const workFailed = new Error("the agent step failed");
  await assert.rejects(
    pool.lease("agents/42.sqlite", { offload: true }, (path) => {
      assert.equal(countItems(path), 5, "the offloaded database came back with another row count");
      addItems(path, 1);
      throw workFailed;
    }),
    (error: unknown) => error === workFailed,
  );
  assert.equal((await worker.databases.writer("agents/42.sqlite")).thisMachine, true, "after failed work the lease is gone");
  await pool.close();
  status = await worker.databases.get("agents/42.sqlite");
  assert.notEqual(status.state, "offloaded", "the release after failed work offloaded the local copy");

  // A failed close does not release: the database stays acquired here.
  const closeFailed = new Error("a connection is still open");
  pool = worker.databases.pool({ adapter: pathAdapter(closeFailed) });
  await pool.lease("agents/42.sqlite", {}, () => {});
  await assert.rejects(pool.release("agents/42.sqlite"), (error: unknown) =>
    error instanceof LeaseCleanupError && error.step === "close" && error.code === ErrorCode.leaseCleanupFailed && error.cause === closeFailed);
  assert.equal((await worker.databases.writer("agents/42.sqlite")).thisMachine, true, "after a failed close the lock moved");
  await worker.databases.release("agents/42.sqlite", { durability: "server" });

  // A release that times out on an open handle is a cleanup failure with its durability.
  pool = worker.databases.pool({ adapter: pathAdapter(), cleanupTimeout: "2s" });
  await pool.lease("agents/42.sqlite", { durability: "server" }, (path) => keepOpen(t, path));
  await assert.rejects(pool.close(), (error: unknown) =>
    error instanceof LeaseCleanupError && error.step === "release" && error.details.durability === "server" &&
    error.cause instanceof LitebackupError && error.cause.code === ErrorCode.stillOpen);
});

test("backs up and acquires empty files as new databases", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const web1 = await server.connect("web-1");
  const web2 = await server.connect("web-2");

  const emptyPath = server.path("web-1", "empty.db");
  new DatabaseSync(emptyPath).close();
  const empty = await web1.backups.start(emptyPath);
  assert.equal((await web1.databases.get("empty.db")).syncedServer.version, 0);
  addItems(emptyPath, 1);
  await web1.databases.sync(empty.name);
  assert.equal((await web1.databases.get("empty.db")).syncedServer.version, 1);

  const tenantPath = server.path("web-1", "7.db");
  new DatabaseSync(tenantPath).close();
  const created = await web1.databases.acquire("tenants/7.db", { path: tenantPath });
  assert.equal(created.decision, "created");
  addItems(tenantPath, 3);
  await web1.databases.release("tenants/7.db", { durability: "server" });
  const movedPath = server.path("web-2", "7.db");
  new DatabaseSync(movedPath).close();
  const moved = await web2.databases.acquire("tenants/7.db", { path: movedPath });
  assert.equal(moved.decision, "downloaded");
  assert.equal(countItems(movedPath), 3);
});

test("prefix replicas download existing and future databases and stop as a group", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const origin = await server.connect("prefix-origin");
  const reader = await server.connect("prefix-reader");
  const firstPath = server.path("prefix-origin", "first.db");
  addItems(firstPath, 2);
  await origin.backups.once(firstPath, { prefix: "tenants/" });
  const directory = server.path("prefix-reader", "replicas");
  mkdirSync(directory, { recursive: true });
  const prefix = await reader.replicas.startPrefix("tenants", { directory: directory });
  assert.equal(prefix.prefix, "tenants");
  const firstCopy = join(directory, "first.db");
  await eventually(30_000, async () => {
    assert.ok(existsSync(firstCopy));
    assert.equal(countItems(firstCopy), 2);
  });
  const secondPath = server.path("prefix-origin", "second.db");
  addItems(secondPath, 3);
  await origin.backups.once(secondPath, { prefix: "tenants/" });
  const secondCopy = join(directory, "second.db");
  await eventually(30_000, async () => {
    assert.ok(existsSync(secondCopy));
    assert.equal(countItems(secondCopy), 3);
  });
  await reader.replicas.stopPrefix("tenants", directory);
  assert.equal((await reader.replicas.list()).prefixes.length, 0);
  const writable = new DatabaseSync(firstCopy);
  writable.exec("INSERT INTO items (body) VALUES ('local after stop')");
  writable.close();
  assert.equal(countItems(firstCopy), 3);
});

test("documented one-shot runners archive their files and leave no backup rules", { skip, timeout: 120_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const job = await server.connect("job");
  const environment = { ...process.env, LITEBACKUP_SESSION: server.session("job") };
  const jsPath = server.path("job", "js-input.db");
  addItems(jsPath, 4);
  await executeFile(process.execPath, [new URL("../examples/one-shot.js", import.meta.url).pathname, jsPath], { env: environment });
  assert.deepEqual(await job.rules.list(), []);
  const jsRestored = server.path("restored", "js-input.db");
  await job.databases.restore("scheduled/js-input.db", { path: jsRestored });
  assert.equal(countItems(jsRestored), 4);

  const binDirectory = server.path("bin");
  mkdirSync(binDirectory, { recursive: true });
  await symlink(binary!, join(binDirectory, "litebackup"));
  const cliPath = server.path("job", "cli-input.db");
  addItems(cliPath, 5);
  await executeFile("bash", [new URL("../../../examples/cron/backup-sqlite.sh", import.meta.url).pathname, cliPath], {
    env: { ...environment, PATH: `${binDirectory}:${process.env.PATH}` },
  });
  assert.deepEqual(await job.rules.list(), []);
  const cliRestored = server.path("restored", "cli-input.db");
  await job.databases.restore("scheduled/cli-input.db", { path: cliRestored });
  assert.equal(countItems(cliRestored), 5);

  // A one-shot job must not stop a continuous backup of the same file.
  const continuousPath = server.path("job", "continuous.db");
  addItems(continuousPath, 2);
  const continuous = await job.backups.start(continuousPath);
  await job.databases.sync(continuous.name);
  await assert.rejects(
    executeFile("bash", [new URL("../../../examples/cron/backup-sqlite.sh", import.meta.url).pathname, continuousPath], {
      env: { ...environment, PATH: `${binDirectory}:${process.env.PATH}` },
    }),
    (error: unknown) => {
      const failure = error as { stderr: string };
      return failure.stderr.includes("busy") && !failure.stderr.includes("Could not stop backup");
    },
  );
  await assert.rejects(executeFile(process.execPath, [new URL("../examples/one-shot.js", import.meta.url).pathname, continuousPath], { env: environment }));
  assert.deepEqual((await job.rules.list()).map((backup) => backup.id), [continuous.id]);
  await job.databases.sync(continuous.name);
  assert.deepEqual((await job.rules.list()).map((backup) => backup.id), [continuous.id]);
  await job.backups.stop(continuous.name);
});

test("foreground recursive backup reports bad files and preserves relative names", { skip, timeout: 30_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const reader = await server.connect("reader");
  const session = server.session("scheduled");
  const token = await server.joinToken(session);
  const worker = new Litebackup({ session, apiKey: token });
  const root = server.path("source");
  addItems(join(root, "Équipe", "42.sqlite"), 2);
  addItems(join(root, "Main.sqlite"), 3);
  const rootLink = server.path("source-link");
  await symlink(root, rootLink);
  await symlink(root, join(root, "cycle"));
  await writeFile(join(root, "invalid.sqlite"), "invalid");
  await assert.rejects(
    worker.backups.once(join(rootLink, "**", "*.sqlite"), { prefix: "租户/" }),
    (err: unknown) => err instanceof LitebackupError && err.message.includes("could not be backed up") && err.message.includes("invalid.sqlite"),
  );
  for (const name of ["租户/Équipe/42.sqlite", "租户/Main.sqlite"]) {
    assert.equal((await reader.databases.versions(name)).length, 1);
  }
  const source = join(root, "Main.sqlite");
  const exact = "Mixed Case/É.sqlite";
  await worker.backups.once(source, { name: exact, durability: "cloud" });
  const restored = server.path("result", "Exact.sqlite");
  await reader.databases.restore(exact, { path: restored });
  assert.equal(countItems(restored), 3);
  const controller = new AbortController();
  const pending = worker.backups.once(source, { name: "cancel.sqlite", durability: "cloud", signal: controller.signal });
  const cancelled = assert.rejects(pending, (err: unknown) => err instanceof LitebackupError && err.code === "cancelled");
  await eventually(3_000, async () => {
    assert.ok((await reader.databases.writer("cancel.sqlite")).active);
  });
  controller.abort();
  await cancelled;
  assert.equal((await reader.databases.writer("cancel.sqlite")).active, false);
  assert.equal(existsSync(sessionSocket(session, process.env).socketPath), false);
});

test("stop selects single files by remote name and whole backup rules by ID", { skip, timeout: 30_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const client = await server.connect("rules");
  const env = { ...process.env, LITEBACKUP_SESSION: server.session("rules") };
  const cli = (...args: string[]) => executeFile(binary!, args, { cwd: server.directory, env });
  const path = server.path("source", "local.sqlite");
  addItems(path, 2);
  const single = await client.backups.start(path, { prefix: "Équipe/" });
  assert.equal(single.name, "Équipe/local.sqlite");
  await client.databases.sync(single.name);
  await rm(path);
  assert.equal((await client.rules.list())[0]?.name, single.name);
  await assert.rejects(cli("stop", path));
  await cli("stop", single.name);
  assert.ok((await client.databases.versions(single.name)).length > 0);

  addItems(path, 1);
  const siblingPath = server.path("source", "other.sqlite");
  addItems(siblingPath, 1);
  const group = await client.backups.start(join(dirname(path), "**", "*.sqlite"), { prefix: "Équipe/" });
  const empty = await client.backups.start(join(dirname(path), "*.empty"), { prefix: "Équipe/" });
  await eventually(5_000, async () => {
    assert.deepEqual((await client.databases.list()).map((db) => db.name), [single.name, "Équipe/other.sqlite"]);
  });
  await assert.rejects(client.backups.stop(single.name), (err: unknown) =>
    err instanceof LitebackupError && err.code === "invalid_argument" && err.message.includes(group.id));
  await assert.rejects(cli("stop", "--rule", group.id, single.name));
  assert.equal((await client.rules.list()).length, 2);
  assert.equal((await client.databases.list()).length, 2);
  await cli("stop", "--rule", group.id);
  assert.deepEqual((await client.rules.list()).map((rule) => rule.id), [empty.id]);
  assert.deepEqual(await client.databases.list(), []);
  await client.rules.stop(empty.id);
  assert.deepEqual(await client.rules.list(), []);
});

test("a killed foreground job loses its writer lease and saves no identity", { skip, timeout: 30_000 }, async (t) => {
  const server = await DevelopmentServer.start(t);
  const reader = await server.connect("reader");
  const session = server.session("killed");
  const token = await server.joinToken(session);
  const path = server.path("source", "kill.sqlite");
  addItems(path, 1);
  const child = spawn(binary!, ["backup", "--durability", "cloud", "--session", session, "--", path], {
    env: { ...process.env, LITEBACKUP_API_KEY: token }, stdio: "ignore",
  });
  const done = new Promise((resolve) => child.once("exit", resolve));
  t.after(() => { child.kill("SIGKILL"); });
  await eventually(5000, async () => assert.ok((await reader.databases.writer("kill.sqlite")).active));
  assert.ok(child.kill("SIGKILL"));
  await done;
  await eventually(8000, async () => assert.equal((await reader.databases.writer("kill.sqlite")).active, false));
  assert.equal(existsSync(join(process.env.XDG_STATE_HOME!, "litebackup", session, "credentials.json")), false);
  await new Litebackup({ session, apiKey: token }).backups.once(path);
});
