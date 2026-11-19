// The driver subpath exports, against the real better-sqlite3, node:sqlite, and Drizzle.
import assert from "node:assert/strict";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
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

test("better-sqlite3: the adapter rolls back unfinished work and never commits", async (t) => {
  const directory = await workingDirectory(t);
  const path = join(directory, "scope.sqlite");
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
  assert.equal((check.prepare("SELECT count(*) AS rows FROM steps").get() as { rows: number }).rows, 0);
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
  const path = join(directory, "scope.sqlite");
  const adapter = nodeSqlite.adapter();
  const db = await adapter.open(path);
  db.exec("CREATE TABLE steps (id INTEGER)");
  db.exec("BEGIN");
  db.exec("INSERT INTO steps VALUES (1)");
  await adapter.close(db);
  assert.equal(db.isOpen, false);
  const check = new DatabaseSync(path);
  t.after(() => check.close());
  assert.equal((check.prepare("SELECT count(*) AS rows FROM steps").get() as { rows: number }).rows, 0);
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
