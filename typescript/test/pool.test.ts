// The pool against a fake daemon: leases keep their lock between calls, a lost
// lock ends them, idle ones are released, and use() keeps a backup rule's
// database open.
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test, type TestContext } from "node:test";

import { ErrorCode, LeaseCleanupError, Litebackup, LitebackupError, type Adapter } from "../src/index.js";
import { poolTiming } from "../src/pool.js";
import { FakeDaemon, type FakeRequest } from "./fake.js";

/** A fake daemon for one leased or backed-up database: it logs each database request as a step. */
async function leaseDaemon(t: TestContext, status: Record<string, unknown>) {
  const daemon = await FakeDaemon.start(t);
  const steps: string[] = [];
  const step = (name: string, answer: (request: FakeRequest) => unknown) => (request: FakeRequest): [number, unknown] => {
    steps.push(name);
    return [200, answer(request)];
  };
  daemon.route("POST /v1/databases/acquire", step("acquire", () => ({ database: { ...status, state: "syncing" }, decision: "downloaded" })));
  daemon.route("POST /v1/databases/release", step("release", () => ({ database: status, checkpointed: false, vacuumed: false, offloaded: false })));
  daemon.route("POST /v1/databases/sync", step("sync", () => status));
  daemon.route("GET /v1/databases/status", step("status", () => status));
  daemon.route("GET /v1/backup-rules/list", step("rules", () => ({ rules: [] })));
  const client = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => client.close());
  return {
    daemon,
    client,
    steps,
    status,
    /** An adapter that logs open and close as steps. */
    adapter(): Adapter<string> {
      return {
        open: () => {
          steps.push("open");
          return "db";
        },
        close: () => {
          steps.push("close");
        },
      };
    },
  };
}

/** The status of a database this machine acquired. */
function acquiredStatus(path: string): Record<string, unknown> {
  return {
    name: "agents/7.sqlite", path, role: "origin", state: "synced", acquired: true, failover: false, local_changed: false,
    synced_server: { version: 3 }, synced_cloud: { version: 3 }, journal_mode: "wal", page_count: 2, sync_count: 3, hint_misses: 0,
  };
}

/** The status of a backup rule's database. */
function ruleStatus(path: string, state: string): Record<string, unknown> {
  return { ...acquiredStatus(path), name: "tenants/42.sqlite", acquired: false, state, backup_rule_id: "rule-1" };
}

/** A scratch directory removed after the test. */
async function scratchDirectory(t: TestContext): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), "lb-pool-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

/** Resolves once check passes, or fails after timeoutMillis. */
async function eventually(timeoutMillis: number, check: () => boolean, message: () => string): Promise<void> {
  const deadline = Date.now() + timeoutMillis;
  while (!check()) {
    if (Date.now() > deadline) assert.fail(message());
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

const isCode = (code: string) => (error: unknown) => error instanceof LitebackupError && error.code === code;

test("a lease keeps its lock and handle between calls, and syncs only with a durability", async (t) => {
  const path = join(await scratchDirectory(t), "7.sqlite");
  const lease = await leaseDaemon(t, acquiredStatus(path));
  const pool = lease.client.databases.pool({ adapter: lease.adapter() });
  for (const durability of [undefined, "server", undefined] as const) {
    assert.equal(await pool.lease("agents/7.sqlite", { path, durability }, () => {
      lease.steps.push("work");
      return 7;
    }), 7);
  }
  await pool.close();
  // Each later call asks whether the lock is still this machine's.
  assert.deepEqual(lease.steps, ["acquire", "open", "work", "status", "work", "sync", "status", "work", "close", "release"]);
  assert.equal(lease.daemon.received("/v1/events/list").length, 0);
  await assert.rejects(pool.lease("agents/7.sqlite", {}, () => {}), isCode(ErrorCode.closed));
});

test("calls on one leased name run one at a time", async (t) => {
  const path = join(await scratchDirectory(t), "7.sqlite");
  const lease = await leaseDaemon(t, acquiredStatus(path));
  const pool = lease.client.databases.pool({ adapter: lease.adapter() });
  let running = 0;
  let mostRunning = 0;
  await Promise.all([1, 2, 3].map(() => pool.lease("agents/7.sqlite", { path }, async () => {
    running++;
    mostRunning = Math.max(mostRunning, running);
    await new Promise((resolve) => setTimeout(resolve, 20));
    running--;
  })));
  assert.equal(mostRunning, 1);
  assert.equal(lease.steps.filter((step) => step === "acquire").length, 1);
  await pool.close();
});

test("a lease whose lock was lost or whose copy is conflicted rejects with lock_lost without work, and the next one acquires again", async (t) => {
  for (const lost of [{ acquired: false }, { state: "conflicted" }]) {
    const path = join(await scratchDirectory(t), "7.sqlite");
    const lease = await leaseDaemon(t, acquiredStatus(path));
    const pool = lease.client.databases.pool({ adapter: lease.adapter() });
    await pool.lease("agents/7.sqlite", { path }, () => {});
    Object.assign(lease.status, lost);
    await assert.rejects(pool.lease("agents/7.sqlite", { path }, () => assert.fail("the work ran")), isCode(ErrorCode.lockLost));
    Object.assign(lease.status, { acquired: true, state: "synced" });
    await pool.lease("agents/7.sqlite", { path }, () => {});
    assert.deepEqual(lease.steps, ["acquire", "open", "status", "close", "acquire", "open"], JSON.stringify(lost));
    await pool.close();
  }
});

test("a lease whose release failed after its close reopens the handle at its path", async (t) => {
  const path = join(await scratchDirectory(t), "7.sqlite");
  const lease = await leaseDaemon(t, acquiredStatus(path));
  const opened: string[] = [];
  const adapter: Adapter<string> = {
    open: (openPath) => {
      lease.steps.push("open");
      opened.push(openPath);
      return `db ${opened.length}`;
    },
    close: () => {
      lease.steps.push("close");
    },
  };
  const pool = lease.client.databases.pool({ adapter, idleRelease: 0 });
  await pool.lease("agents/7.sqlite", { path }, () => {});
  lease.daemon.route("POST /v1/databases/release", () => {
    lease.steps.push("release");
    return [503, { code: "unavailable", message: "release failed" }];
  });
  await assert.rejects(pool.release("agents/7.sqlite"), (error: unknown) => error instanceof LeaseCleanupError && error.step === "release");
  lease.daemon.route("POST /v1/databases/release", () => {
    lease.steps.push("release");
    return [200, { database: lease.status, checkpointed: false, vacuumed: false, offloaded: false }];
  });
  assert.equal(await pool.lease("agents/7.sqlite", { path }, (db) => db), "db 2");
  assert.deepEqual(opened, [path, path]);
  await pool.close();
  assert.deepEqual(lease.steps, ["acquire", "open", "close", "release", "status", "open", "close", "release"]);
});

test("close waits for running calls, refuses new ones, and only then closes handles", async (t) => {
  const path = join(await scratchDirectory(t), "42.sqlite");
  const lease = await leaseDaemon(t, ruleStatus(path, "synced"));
  const pool = lease.client.databases.pool({ adapter: lease.adapter() });
  let finishWork!: () => void;
  const workFinished = new Promise<void>((resolve) => {
    finishWork = resolve;
  });
  let workStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    workStarted = resolve;
  });
  const using = pool.use("tenants/42.sqlite", async () => {
    workStarted();
    await workFinished;
    lease.steps.push("work done");
  });
  await started;
  let closed = false;
  const closing = pool.close().then(() => {
    closed = true;
  });
  await assert.rejects(pool.use("tenants/42.sqlite", () => {}), isCode(ErrorCode.closed));
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(closed, false);
  assert.ok(!lease.steps.includes("close"), String(lease.steps));
  finishWork();
  await using;
  await closing;
  assert.deepEqual(lease.steps.slice(-2), ["work done", "close"]);
});

test("idleRelease defaults to a minute, and 0 keeps the lease", async (t) => {
  const idleCheckMillis = poolTiming.idleCheckMillis;
  poolTiming.idleCheckMillis = 10;
  t.after(() => {
    poolTiming.idleCheckMillis = idleCheckMillis;
  });
  for (const [idleRelease, released] of [[undefined, true], ["0s", false]] as const) {
    const path = join(await scratchDirectory(t), "7.sqlite");
    const lease = await leaseDaemon(t, acquiredStatus(path));
    const pool = lease.client.databases.pool({ adapter: lease.adapter(), idleRelease });
    await pool.lease("agents/7.sqlite", { path, durability: "cloud" }, () => {});
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.ok(!lease.steps.includes("release"), "released before a minute passed");
    // A minute later by the clock the pool reads.
    const now = Date.now;
    Date.now = () => now() + 61_000;
    try {
      await new Promise((resolve) => setTimeout(resolve, 100));
    } finally {
      Date.now = now;
    }
    assert.equal(lease.steps.includes("release"), released, `idleRelease ${idleRelease}: ${lease.steps}`);
    if (released) {
      assert.deepEqual(lease.steps.slice(-2), ["close", "release"]);
      assert.equal(lease.daemon.received("/v1/databases/release")[0]!.body!.durability, "cloud");
    }
    await pool.close();
  }
});

test("the release gets what closing left of the cleanup timeout", async (t) => {
  const path = join(await scratchDirectory(t), "7.sqlite");
  const lease = await leaseDaemon(t, acquiredStatus(path));
  const adapter: Adapter<string> = { open: () => "db", close: () => new Promise<void>((resolve) => setTimeout(resolve, 500)) };
  const pool = lease.client.databases.pool({ adapter, cleanupTimeout: 2000 });
  await pool.lease("agents/7.sqlite", { path }, () => {});
  await pool.close();
  const [release] = lease.daemon.received("/v1/databases/release");
  const timeout = (release?.body as { timeout_millis: number }).timeout_millis;
  assert.ok(timeout >= 1000 && timeout <= 1600, `release timeout ${timeout} ms, want what the 500 ms close left of 2 s`);
});

test("pool.release ends a lease, and a failed close is a close LeaseCleanupError that keeps it", async (t) => {
  const path = join(await scratchDirectory(t), "7.sqlite");
  const lease = await leaseDaemon(t, acquiredStatus(path));
  let closeFails = true;
  const closeError = new Error("a connection is still open");
  const adapter: Adapter<string> = {
    open: () => "db",
    close: () => {
      if (closeFails) throw closeError;
    },
  };
  const pool = lease.client.databases.pool({ adapter });
  await assert.rejects(pool.release("agents/7.sqlite"), isCode(ErrorCode.invalidArgument));
  await pool.lease("agents/7.sqlite", { path, offload: true }, () => {});
  await assert.rejects(pool.release("agents/7.sqlite"), (error: unknown) =>
    error instanceof LeaseCleanupError && error.step === "close" && error.code === ErrorCode.leaseCleanupFailed && error.cause === closeError);
  assert.equal(lease.daemon.received("/v1/databases/release").length, 0);
  closeFails = false;
  await pool.release("agents/7.sqlite");
  assert.deepEqual(lease.daemon.received("/v1/databases/release")[0]!.body, { name: "agents/7.sqlite", durability: "server", offload: true, timeout_millis: lease.daemon.received("/v1/databases/release")[0]!.body!.timeout_millis });
  await pool.close();
});

test("idleRelease releases an unused lease, and a failed release goes to onReleaseError", async (t) => {
  const idleCheckMillis = poolTiming.idleCheckMillis;
  poolTiming.idleCheckMillis = 10;
  t.after(() => {
    poolTiming.idleCheckMillis = idleCheckMillis;
  });
  const path = join(await scratchDirectory(t), "7.sqlite");
  const lease = await leaseDaemon(t, acquiredStatus(path));
  lease.daemon.route("POST /v1/databases/release", () => [503, { code: "unavailable", message: "release failed" }]);
  const reported: LeaseCleanupError[] = [];
  const pool = lease.client.databases.pool({ adapter: lease.adapter(), idleRelease: 50, onReleaseError: (error) => reported.push(error) });
  await pool.lease("agents/7.sqlite", { path }, () => {});
  await eventually(5000, () => reported.length > 0, () => "no failed release was reported");
  assert.equal(reported[0]!.step, "release");
  assert.equal(reported[0]!.details.durability, "server");
  lease.daemon.route("POST /v1/databases/release", () => [200, { database: lease.status, checkpointed: false, vacuumed: false, offloaded: false }]);
  await pool.close();
});

test("a lease whose directory cannot be created is invalid_argument with the cause", async (t) => {
  const directory = await scratchDirectory(t);
  await writeFile(join(directory, "file"), "");
  const lease = await leaseDaemon(t, acquiredStatus(join(directory, "7.sqlite")));
  const pool = lease.client.databases.pool({ adapter: { open: () => assert.fail("opened"), close: () => {} } });
  // A file in the middle of the directory path, and a file where the directory itself should be.
  for (const [path, code] of [
    [join(directory, "file", "sub", "app.db"), "ENOTDIR"],
    [join(directory, "file", "app.db"), "EEXIST"],
  ]) {
    await assert.rejects(
      pool.lease("app.db", { path }, () => assert.fail("ran")),
      (error: unknown) => isCode(ErrorCode.invalidArgument)(error) && ((error as Error).cause as NodeJS.ErrnoException)?.code === code,
    );
  }
  assert.equal(lease.daemon.received("/v1/databases/acquire").length, 0);
  await pool.close();
});

test("a pool refuses a cleanup timeout of zero, a missing adapter, and a lease durability of none", async (t) => {
  const lease = await leaseDaemon(t, acquiredStatus("/var/lib/app/7.sqlite"));
  assert.throws(() => lease.client.databases.pool({ adapter: lease.adapter(), cleanupTimeout: 0 }), isCode(ErrorCode.invalidArgument));
  assert.throws(() => lease.client.databases.pool({} as never), isCode(ErrorCode.invalidArgument));
  const pool = lease.client.databases.pool({ adapter: lease.adapter() });
  await assert.rejects(pool.lease("agents/7.sqlite", { durability: "none" as "server" }, () => {}), isCode(ErrorCode.invalidArgument));
  await assert.rejects(pool.lease("", {}, () => {}), isCode(ErrorCode.invalidArgument));
  assert.deepEqual(lease.steps, []);
  await pool.close();
});

test("use brings an offloaded rule database back once, keeps it open, and refuses the wrong calls", async (t) => {
  const path = join(await scratchDirectory(t), "42.sqlite");
  const lease = await leaseDaemon(t, ruleStatus(path, "offloaded"));
  const pool = lease.client.databases.pool({ adapter: lease.adapter() });
  let calls = 0;
  await Promise.all([1, 2, 3, 4].map(() => pool.use("tenants/42.sqlite", (db) => {
    assert.equal(db, "db");
    calls++;
  })));
  assert.equal(calls, 4);
  assert.equal(lease.steps.filter((step) => step === "open").length, 1, String(lease.steps));
  assert.equal(lease.steps.filter((step) => step === "acquire").length, 1, String(lease.steps));
  await assert.rejects(pool.lease("tenants/42.sqlite", {}, () => {}), isCode(ErrorCode.invalidArgument));
  Object.assign(lease.status, { name: "agents/7.sqlite", acquired: true, backup_rule_id: undefined });
  await assert.rejects(pool.use("agents/7.sqlite", () => {}), isCode(ErrorCode.invalidArgument));
  await pool.close();
  assert.equal(lease.steps.filter((step) => step === "close").length, 1);
  assert.equal(lease.daemon.received("/v1/databases/release").length, 0);
});

test("an idle rule database closes only when idleClose or its rule says so", async (t) => {
  const idleCheckMillis = poolTiming.idleCheckMillis;
  poolTiming.idleCheckMillis = 10;
  t.after(() => {
    poolTiming.idleCheckMillis = idleCheckMillis;
  });
  for (const [idleClose, closed] of [[undefined, false], [50, true]] as const) {
    const path = join(await scratchDirectory(t), "42.sqlite");
    await writeFile(path, "");
    const lease = await leaseDaemon(t, ruleStatus(path, "synced"));
    const pool = lease.client.databases.pool({ adapter: lease.adapter(), idleClose });
    await pool.use("tenants/42.sqlite", () => {});
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(lease.steps.includes("close"), closed, `idleClose ${idleClose}: ${lease.steps}`);
    // Without idleClose, the pool asked for the rule to see whether it offloads.
    assert.equal(lease.steps.includes("rules"), idleClose === undefined);
    await pool.close();
  }
});

test("a failed idle close keeps the rule database's handle, so the next check and close retry", async (t) => {
  const idleCheckMillis = poolTiming.idleCheckMillis;
  poolTiming.idleCheckMillis = 10;
  t.after(() => {
    poolTiming.idleCheckMillis = idleCheckMillis;
  });
  const path = join(await scratchDirectory(t), "42.sqlite");
  await writeFile(path, "");
  const lease = await leaseDaemon(t, ruleStatus(path, "synced"));
  let closeFails = true;
  const closedHandles: string[] = [];
  const adapter: Adapter<string> = {
    open: () => {
      lease.steps.push("open");
      return "db";
    },
    close: (db) => {
      lease.steps.push("close");
      if (closeFails) throw new Error("a statement is still running");
      closedHandles.push(db);
    },
  };
  const pool = lease.client.databases.pool({ adapter, idleClose: 20 });
  await pool.use("tenants/42.sqlite", () => {});
  await eventually(5000, () => lease.steps.filter((step) => step === "close").length >= 2, () => `the idle close was not retried: ${lease.steps}`);
  // The entry still holds the handle: a call reuses it without opening again.
  await pool.use("tenants/42.sqlite", (db) => assert.equal(db, "db"));
  assert.equal(lease.steps.filter((step) => step === "open").length, 1);
  closeFails = false;
  await pool.close();
  assert.deepEqual(closedHandles, ["db"]);
});

test("a rollback-journal database under a rule that offloads it closes after its last use; others stay open", async (t) => {
  const cases = [
    { journalMode: "rollback", offloadRollbackJournal: true, closed: true },
    { journalMode: "rollback", offloadRollbackJournal: false, closed: false },
    { journalMode: "wal", offloadRollbackJournal: true, closed: false },
  ];
  for (const { journalMode, offloadRollbackJournal, closed } of cases) {
    const path = join(await scratchDirectory(t), "42.sqlite");
    await writeFile(path, "");
    const lease = await leaseDaemon(t, { ...ruleStatus(path, "synced"), journal_mode: journalMode });
    lease.daemon.route("GET /v1/backup-rules/list", () => {
      lease.steps.push("rules");
      return [200, { rules: [{ id: "rule-1", path, maximum_local_databases: 0, offload_rollback_journal: offloadRollbackJournal }] }];
    });
    const pool = lease.client.databases.pool({ adapter: lease.adapter() });
    const description = `${journalMode}, offload_rollback_journal ${offloadRollbackJournal}`;
    // Two calls at once share the handle; it closes only after the last returns.
    await Promise.all([1, 2].map(() => pool.use("tenants/42.sqlite", () => new Promise((resolve) => setTimeout(resolve, 20)))));
    await eventually(2000, () => !closed || lease.steps.includes("close"), () => `${description}: never closed: ${lease.steps}`);
    await pool.use("tenants/42.sqlite", () => {});
    await new Promise((resolve) => setTimeout(resolve, 50));
    const opens = lease.steps.filter((step) => step === "open").length;
    const closes = lease.steps.filter((step) => step === "close").length;
    assert.deepEqual([opens, closes], closed ? [2, 2] : [1, 0], `${description}: ${lease.steps}`);
    assert.equal(lease.steps.filter((step) => step === "rules").length, 1, `${description}: the rule is read once`);
    await pool.close();
  }
});

test("a failed close after the last use goes to onReleaseError and keeps the handle", async (t) => {
  const path = join(await scratchDirectory(t), "42.sqlite");
  await writeFile(path, "");
  const lease = await leaseDaemon(t, { ...ruleStatus(path, "synced"), journal_mode: "rollback" });
  lease.daemon.route("GET /v1/backup-rules/list", () => [200, { rules: [{ id: "rule-1", path, maximum_local_databases: 0, offload_rollback_journal: true }] }]);
  let closeFails = true;
  const adapter: Adapter<string> = {
    open: () => {
      lease.steps.push("open");
      return "db";
    },
    close: () => {
      lease.steps.push("close");
      if (closeFails) throw new Error("a statement is still running");
    },
  };
  const reported: LeaseCleanupError[] = [];
  const pool = lease.client.databases.pool({ adapter, onReleaseError: (error) => reported.push(error) });
  await pool.use("tenants/42.sqlite", () => {});
  await eventually(2000, () => reported.length > 0, () => "the failed close was not reported");
  assert.equal(reported[0]!.step, "close");
  assert.equal(reported[0]!.details.acquired, false);
  closeFails = false;
  await pool.close();
  assert.deepEqual(lease.steps.filter((step) => step === "open" || step === "close"), ["open", "close", "close"]);
});
