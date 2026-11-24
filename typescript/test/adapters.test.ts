// The driver subpath exports, against the real better-sqlite3, node:sqlite, and Drizzle.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readdir, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { DatabaseSync } from "node:sqlite";
import { test, type TestContext } from "node:test";

import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";

import * as betterSqlite3 from "../src/better-sqlite3.js";
import * as drizzleSupport from "../src/drizzle.js";
import { LitebackupError } from "../src/index.js";
import * as nodeSqlite from "../src/node-sqlite.js";

/** A working directory for one test, its symlinks resolved; the previous one comes back after. */
async function workingDirectory(t: TestContext): Promise<string> {
  const directory = await realpath(await mkdtemp(join(tmpdir(), "lb-adapters-")));
  const previous = process.cwd();
  process.chdir(directory);
  t.after(async () => {
    process.chdir(previous);
    await rm(directory, { recursive: true, force: true });
  });
  return directory;
}

const invalid = (error: unknown) => error instanceof LitebackupError && error.code === "invalid_argument";

test("better-sqlite3: relative paths, attached databases, and in-memory refusal", async (t) => {
  const directory = await workingDirectory(t);
  const db = new Database("data.sqlite");
  t.after(() => db.close());
  assert.equal(betterSqlite3.databasePath(db), join(directory, "data.sqlite"));
  db.exec(`ATTACH DATABASE '${join(directory, "other.sqlite")}' AS other`);
  assert.equal(betterSqlite3.databasePath(db, { schema: "other" }), join(directory, "other.sqlite"));
  db.exec("ATTACH DATABASE ':memory:' AS scratch");
  assert.throws(() => betterSqlite3.databasePath(db, { schema: "scratch" }), invalid);
  assert.throws(() => betterSqlite3.databasePath(db, { schema: "missing" }), invalid);
  const memory = new Database(":memory:");
  t.after(() => memory.close());
  assert.throws(() => betterSqlite3.databasePath(memory), invalid);
  const temporary = new Database("");
  t.after(() => temporary.close());
  assert.throws(() => betterSqlite3.databasePath(temporary), invalid);
});

test("better-sqlite3: databasePath reports the opened file after the working directory changes", async (t) => {
  const directory = await workingDirectory(t);
  const db = new Database("data.sqlite");
  t.after(() => db.close());
  await mkdir(join(directory, "elsewhere"));
  process.chdir(join(directory, "elsewhere"));
  assert.equal(betterSqlite3.databasePath(db), join(directory, "data.sqlite"));
});

test("better-sqlite3: the adapter refuses a path with surrounding whitespace and creates nothing", async (t) => {
  const directory = await workingDirectory(t);
  const adapter = betterSqlite3.adapter(Database);
  for (const path of [" a.db", "a.db ", join(directory, "b.db") + "\n"]) {
    assert.throws(() => adapter.open(path), invalid);
  }
  assert.deepEqual(await readdir(directory), []);
});

test("better-sqlite3: the adapter rolls back unfinished work and never commits", async (t) => {
  const directory = await workingDirectory(t);
  const path = join(directory, "lease.sqlite");
  await writeFile(path, "");
  const adapter = betterSqlite3.adapter(Database);
  const db = await adapter.open(path);
  db.exec("CREATE TABLE steps (id INTEGER)");
  db.exec("BEGIN");
  db.prepare("INSERT INTO steps VALUES (1)").run();
  assert.equal(db.inTransaction, true);
  await adapter.close(db);
  assert.equal(db.open, false);
  const check = new Database(path);
  t.after(() => check.close());
  assert.equal((check.prepare("SELECT count(*) AS steps FROM steps").get() as { steps: number }).steps, 0);
});

test("the adapters open existing files only: a missing one is not_found and stays missing", async (t) => {
  const directory = await workingDirectory(t);
  const notFound = (error: unknown) => error instanceof LitebackupError && error.code === "not_found";
  for (const adapter of [betterSqlite3.adapter(Database), nodeSqlite.adapter()] as { open(path: string): unknown }[]) {
    assert.throws(() => adapter.open(join(directory, "missing.sqlite")), notFound);
    assert.throws(() => adapter.open(join(directory, "absent", "missing.sqlite")), notFound);
  }
  assert.deepEqual(await readdir(directory), []);
});

test("the adapters pass driver options through", async (t) => {
  const directory = await workingDirectory(t);
  const path = join(directory, "options.sqlite");
  new Database(path).exec("CREATE TABLE steps (id INTEGER)");
  const better = betterSqlite3.adapter(Database, { readonly: true });
  const readonlyBetter = await better.open(path);
  assert.throws(() => readonlyBetter.exec("INSERT INTO steps VALUES (1)"), /readonly/);
  await better.close(readonlyBetter);
  const node = nodeSqlite.adapter({ readOnly: true }); // vocabulary:allow node:sqlite's option name
  const readonlyNode = await node.open(path);
  assert.throws(() => readonlyNode.exec("INSERT INTO steps VALUES (1)"), /readonly/);
  await node.close(readonlyNode);
});

test("node:sqlite: location, attached databases, and in-memory refusal", async (t) => {
  const directory = await workingDirectory(t);
  const db = new DatabaseSync("data.sqlite");
  t.after(() => db.close());
  assert.equal(nodeSqlite.databasePath(db), join(directory, "data.sqlite"));
  db.exec(`ATTACH DATABASE 'other.sqlite' AS other`);
  assert.equal(nodeSqlite.databasePath(db, { schema: "other" }), join(directory, "other.sqlite"));
  const memory = new DatabaseSync(":memory:");
  t.after(() => memory.close());
  assert.throws(() => nodeSqlite.databasePath(memory), invalid);
});

test("node:sqlite: the adapter rolls back unfinished work", async (t) => {
  const directory = await workingDirectory(t);
  const path = join(directory, "lease.sqlite");
  await writeFile(path, "");
  const adapter = nodeSqlite.adapter();
  const db = await adapter.open(path);
  db.exec("CREATE TABLE steps (id INTEGER)");
  db.exec("BEGIN");
  db.exec("INSERT INTO steps VALUES (1)");
  await adapter.close(db);
  assert.equal(db.isOpen, false);
  const check = new DatabaseSync(path);
  t.after(() => check.close());
  assert.equal((check.prepare("SELECT count(*) AS steps FROM steps").get() as { steps: number }).steps, 0);
});

test("node:sqlite: the adapter closes and rolls back without isOpen and isTransaction", async () => {
  // Older Node 22 releases lack both properties; hide them behind a stand-in.
  const real = new DatabaseSync(":memory:");
  real.exec("CREATE TABLE steps (id INTEGER)");
  const calls: string[] = [];
  const older = {
    exec: (sql: string) => {
      calls.push(sql);
      real.exec(sql);
    },
    close: () => {
      calls.push("close");
      real.close();
    },
  } as unknown as DatabaseSync;
  const adapter = nodeSqlite.adapter();

  older.exec("BEGIN");
  older.exec("INSERT INTO steps VALUES (1)");
  await adapter.close(older);
  assert.deepEqual(calls, ["BEGIN", "INSERT INTO steps VALUES (1)", "ROLLBACK", "close"]);
  assert.equal(real.isOpen, false);

  // Nothing to roll back, and already closed: both are tolerated.
  calls.length = 0;
  await adapter.close(older);
  assert.deepEqual(calls, ["ROLLBACK", "close"]);
});

test("node:sqlite: the adapter without isTransaction closes a database outside a transaction", async (t) => {
  const directory = await workingDirectory(t);
  const real = new DatabaseSync(join(directory, "plain.sqlite"));
  real.exec("CREATE TABLE steps (id INTEGER); INSERT INTO steps VALUES (1)");
  const older = { exec: (sql: string) => real.exec(sql), close: () => real.close() } as unknown as DatabaseSync;
  await nodeSqlite.adapter().close(older);
  assert.equal(real.isOpen, false);
  const check = new DatabaseSync(join(directory, "plain.sqlite"));
  t.after(() => check.close());
  assert.equal((check.prepare("SELECT count(*) AS steps FROM steps").get() as { steps: number }).steps, 1);
});

test("drizzle: databasePath follows $client to the driver", async (t) => {
  const directory = await workingDirectory(t);
  const db = drizzle("drizzle.sqlite");
  t.after(() => db.$client.close());
  assert.equal(drizzleSupport.databasePath(db), join(directory, "drizzle.sqlite"));
  const nodeClient = new DatabaseSync("node.sqlite");
  t.after(() => nodeClient.close());
  assert.equal(drizzleSupport.databasePath({ $client: nodeClient }), join(directory, "node.sqlite"));
  assert.throws(() => drizzleSupport.databasePath({ $client: { name: "looks.sqlite", memory: false, open: true } }), invalid);
  const memory = drizzle(":memory:");
  t.after(() => memory.$client.close());
  assert.throws(() => drizzleSupport.databasePath(memory), invalid);
});

test("drizzle: better-sqlite3 clients work on a Node without node:sqlite", async (t) => {
  const directory = await workingDirectory(t);
  // Simulates a Node 22 release where node:sqlite is missing or behind a flag.
  const hooks = `export async function resolve(specifier, context, next) {
    if (specifier === "node:sqlite") throw Object.assign(new Error("No such built-in module: node:sqlite"), { code: "ERR_UNKNOWN_BUILTIN_MODULE" });
    return next(specifier, context);
  }`;
  const setup = `import { register } from "node:module";
    register("data:text/javascript," + encodeURIComponent(${JSON.stringify(hooks)}));
    const builtin = process.getBuiltinModule;
    process.getBuiltinModule = (id) => (id === "node:sqlite" || id === "sqlite" ? undefined : builtin(id));`;
  const script = `const { drizzle } = await import(${JSON.stringify(import.meta.resolve("drizzle-orm/better-sqlite3"))});
    const { databasePath } = await import(${JSON.stringify(new URL("../src/drizzle.js", import.meta.url).href)});
    const db = drizzle("drizzle.sqlite");
    console.log(databasePath(db));
    try { databasePath({ $client: {} }); } catch (error) { console.log(error.code); }
    db.$client.close();`;
  const { stdout } = await promisify(execFile)(process.execPath, [
    "--import", "data:text/javascript," + encodeURIComponent(setup),
    "--input-type=module", "--eval", script,
  ], { cwd: directory });
  assert.deepEqual(stdout.trim().split("\n"), [join(directory, "drizzle.sqlite"), "invalid_argument"]);
});
