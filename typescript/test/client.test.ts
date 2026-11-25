import assert from "node:assert/strict";
import { existsSync, lstatSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir, userInfo } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import { ErrorCode, Litebackup, LitebackupError, apiKeyEnvironment, installCommand, litebackupBinary, sdkVersion, sessionSocket } from "../src/index.js";
import { specErrorCodes } from "../src/generated/error_codes.js";
import { foregroundBackup } from "../src/backup.js";
import { failoverTiming } from "../src/failover.js";
import { launchRefusal, startFailureMessage } from "../src/launch.js";
import { FakeDaemon, databaseStatus } from "./fake.js";

/** The refusal a test daemon reports for a failover's file. */
const refusedMessage = "/var/lib/app/main.db differs from the server head; it was left as it is";

test("sends the SDK version, converts answers to camelCase, and maps errors", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("GET /v1/databases/status", (request) => [200, databaseStatus(request.query.get("name")!, "origin", "syncing", 3, 2)]);
  daemon.route("POST /v1/databases/release", () => [409, { code: "still_open", message: "a process still has the database open" }]);
  daemon.route("POST /v1/databases/offload", () => [426, "not json"]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  await litebackup.connect();
  assert.equal(litebackup.clientVersion, "1.5.0");

  const status = await litebackup.databases.get("app/main.db");
  assert.equal(status.syncedServer.version, 3);
  assert.equal(status.syncedCloud.version, 2);
  assert.equal(status.fullScanReason, "first");
  assert.equal(status.localChanged, false);
  assert.equal((status as unknown as Record<string, unknown>).synced_server, undefined);

  await assert.rejects(litebackup.databases.release("app/main.db", { durability: "cloud", timeout: "5s" }), (error: unknown) => {
    assert.ok(error instanceof LitebackupError);
    assert.equal(error.code, "still_open");
    assert.equal(error.status, 409);
    return true;
  });
  assert.deepEqual(daemon.received("/v1/databases/release")[0]!.body, { name: "app/main.db", durability: "cloud", timeout_millis: 5000 });
  await assert.rejects(litebackup.databases.offload("x.db"), (error: unknown) => error instanceof LitebackupError && error.code === "http_426");
  for (const request of daemon.requests) {
    assert.equal(request.headers["litebackup-sdk-version"], `typescript/${sdkVersion}`);
  }
  assert.equal(sdkVersion, "1.5.0");
});

test("startBackup awaits the local rule and stop reports a pending lease", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("POST /v1/backup-rules/start", (req) => [200, { id: "rule", path: req.body!.path }]);
  daemon.route("POST /v1/backup-rules/stop", () => [200, { lease_pending: true }]);
  const client = new Litebackup({ socketPath: daemon.socketPath });
  await client.connect();
  assert.equal((await client.backups.start("My App.db", { prefix: "É/" })).id, "rule");
  await client.backups.start(pathToFileURL(join(process.cwd(), "url dir", "My App.db")));
  assert.deepEqual(
    daemon.received("/v1/backup-rules/start").map((req) => req.body),
    [{ path: join(process.cwd(), "My App.db"), prefix: "É/" }, { path: join(process.cwd(), "url dir", "My App.db") }],
  );
  assert.deepEqual(await client.backups.stop("É/My App.db"), { leasePending: true });
  assert.deepEqual(await client.rules.stop("rule"), { leasePending: true });
  assert.deepEqual(
    daemon.received("/v1/backup-rules/stop").map((req) => req.body),
    [{ name: "É/My App.db" }, { rule_id: "rule" }],
  );
  // A handle is not a path: drivers have their own subpath exports.
  await assert.rejects(client.backups.start({ name: "x.db", memory: false, open: true } as unknown as string), LitebackupError);
  await assert.rejects(client.backups.start(new URL("https://example.com/x.db")), LitebackupError);
});
test("restore, versions, replicate, and acquire send the socket fields", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("GET /v1/databases/versions", (request) =>
    request.query.get("after")
      ? [200, { versions: [{ version: 2, state_digest: "bb", time: "2026-11-15T09:31:00Z" }] }]
      : [200, { versions: [{ version: 1, state_digest: "aa", time: "2026-11-15T09:30:00Z" }], next: 1 }],
  );
  daemon.route("POST /v1/databases/restore", (request) => [200, { name: request.body!.name, path: request.body!.path, version: 2, page_count: 3, page_size: 4096, state_digest: "bb" }]);
  daemon.route("POST /v1/replicas/start", () => [200, { database: databaseStatus("app/main.db", "replica", "syncing", 2, 2) }]);
  daemon.route("POST /v1/databases/acquire", () => [200, { database: databaseStatus("tenants/42.db", "origin", "syncing", 0, 0), decision: "created" }]);
  daemon.route("GET /v1/databases/writer", () => [200, { name: "tenants/42.db", active: true, this_machine: true, writer_machine_label: "web-1", lease_remaining_millis: 5000, lapsed: false }]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  await litebackup.connect();

  const listed = await litebackup.databases.versions("app/main.db");
  assert.deepEqual(listed, [
    { version: 1, stateDigest: "aa", time: "2026-11-15T09:30:00Z" },
    { version: 2, stateDigest: "bb", time: "2026-11-15T09:31:00Z" },
  ]);
  assert.equal(daemon.received("/v1/databases/versions")[1]!.query.get("after"), "1");

  const firstPage = await litebackup.databases.versionsPage("app/main.db", { limit: 1 });
  assert.deepEqual(firstPage, { versions: [{ version: 1, stateDigest: "aa", time: "2026-11-15T09:30:00Z" }], next: 1 });
  const pageRequest = daemon.received("/v1/databases/versions").at(-1)!;
  assert.equal(pageRequest.query.get("limit"), "1");
  assert.equal(pageRequest.query.get("after"), null);
  const nextPage = await litebackup.databases.versionsPage("app/main.db", { after: firstPage.next });
  assert.deepEqual(nextPage.versions, [{ version: 2, stateDigest: "bb", time: "2026-11-15T09:31:00Z" }]);
  const versionNumbers: number[] = [];
  for await (const version of litebackup.databases.allVersions("app/main.db")) versionNumbers.push(version.version);
  assert.deepEqual(versionNumbers, [1, 2]);

  const restored = await litebackup.databases.restore("app/main.db", { path: "/tmp/restored.db", time: new Date("2026-11-15T09:30:30Z") });
  assert.equal(restored.pageSize, 4096);
  assert.deepEqual(daemon.received("/v1/databases/restore")[0]!.body, { name: "app/main.db", path: "/tmp/restored.db", time: "2026-11-15T09:30:30Z", umask: process.umask() });
  await assert.rejects(litebackup.databases.restore("app/main.db", { path: "/tmp/x.db", version: 1, time: "2026-11-15T09:30:30Z" }), LitebackupError);

  const replica = await litebackup.replicas.start("app/main.db", { path: "replica/main.db", writeProtection: "detect", timeout: "2m" });
  assert.equal(replica.role, "replica");
  assert.deepEqual(daemon.received("/v1/replicas/start")[0]!.body, { name: "app/main.db", path: join(process.cwd(), "replica", "main.db"), write_protection: "detect", timeout_millis: 120000 });

  const acquired = await litebackup.databases.acquire("tenants/42.db", { path: "/var/lib/app/42.db", offloadOnRelease: true, machineLabel: "worker-3" });
  assert.equal(acquired.decision, "created");
  assert.deepEqual(daemon.received("/v1/databases/acquire")[0]!.body, { name: "tenants/42.db", path: "/var/lib/app/42.db", offload_on_release: true, machine_label: "worker-3", umask: process.umask() });
  const writer = await litebackup.databases.writer("tenants/42.db");
  assert.equal(writer.thisMachine, true);
  assert.equal(writer.leaseRemainingMillis, 5000);
});

test("failover follows role_changed, draining, and refused events", async (t) => {
  const daemon = await FakeDaemon.start(t);
  const pages: unknown[] = [];
  let waiting: (() => void) | undefined;
  const push = (page: unknown) => {
    pages.push(page);
    waiting?.();
  };
  daemon.route("GET /v1/events/list", async (request) => {
    const after = Number(request.query.get("after") ?? 0);
    if (!request.query.get("wait_millis")) {
      // The cursor read before the start request asks where the log ends.
      assert.equal(after, -1);
      return [200, { events: [], next: 7 }];
    }
    if (pages.length === 0) {
      await new Promise<void>((resolve) => {
        waiting = resolve;
        setTimeout(resolve, 200);
      });
    }
    const page = pages.shift();
    return [200, page ?? { events: [], next: after }];
  });
  daemon.route("POST /v1/failovers/start", (request) => [
    200,
    {
      name: "app/main.db",
      path: request.body!.path,
      role: "replica",
      database: databaseStatus("app/main.db", "replica", "waiting", 3, 3),
      writer: { name: "app/main.db", active: true, this_machine: false, writer_machine_label: "web-1", lapsed: false },
    },
  ]);
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  // While the role is unknown the follower reads it again; the drain goes on.
  daemon.route("GET /v1/databases/status", () => [200, { ...databaseStatus("app/main.db", "origin", "syncing", 3, 3), draining: true }]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  await litebackup.connect();
  t.after(() => litebackup.close());

  const failover = await litebackup.failovers.start("/var/lib/app/main.db");
  assert.deepEqual(daemon.received("/v1/failovers/start")[0]!.body, { path: "/var/lib/app/main.db" });
  assert.equal(failover.role, "replica");
  assert.equal(failover.isOrigin, false);
  assert.equal(failover.status.writer.writerMachineLabel, "web-1");

  const seen: string[] = [];
  const next = (event: "origin" | "replica" | "unknown" | "refused") =>
    new Promise<string>((resolve) => {
      const listener = (path?: string) => {
        failover.off(event, listener);
        resolve(path ?? event);
      };
      failover.on(event, listener);
    });
  failover.on("origin", () => seen.push("origin")).on("replica", () => seen.push("replica"));

  const becameOrigin = next("origin");
  push({
    events: [
      { id: 8, time: "2026-11-15T09:01:00Z", type: "role_changed", message: "origin", name: "other.db", role: "origin" },
      { id: 9, time: "2026-11-15T09:01:00Z", type: "role_changed", message: "origin", name: "app/main.db", role: "origin" },
    ],
    next: 9,
  });
  await becameOrigin;
  assert.equal(failover.isOrigin, true);

  // A drain says this machine stops being the origin before it releases the lock.
  const drained = next("unknown");
  push({
    events: [{ id: 10, time: "2026-11-15T09:01:30Z", type: "draining", message: "this machine is draining", name: "app/main.db" }],
    next: 10,
  });
  await drained;
  assert.equal(failover.role, "unknown");
  assert.equal(failover.isOrigin, false);

  const becameReplica = next("replica");
  const refused = next("refused");
  push({
    events: [
      { id: 11, time: "2026-11-15T09:02:00Z", type: "role_changed", message: "replica", name: "app/main.db", role: "replica" },
      { id: 12, time: "2026-11-15T09:02:00Z", type: "refused", message: refusedMessage, name: "app/main.db", path: "/var/lib/app/main.db" },
    ],
    next: 12,
  });
  await becameReplica;
  assert.equal(await refused, refusedMessage);
  for (const request of daemon.received("/v1/events/list")) {
    if (request.query.get("wait_millis")) {
      assert.ok(Number(request.query.get("after")) >= 7, "the long-poll starts at the end of the log");
    }
  }

  // The role the start answered comes first, on the next turn of the event loop.
  assert.deepEqual(seen, ["replica", "origin", "replica"]);
  await failover.stop();
  assert.equal(failover.role, "unknown");
  assert.deepEqual(daemon.received("/v1/backup-rules/stop")[0]!.body, { name: "app/main.db" });
});

test("failover reads a role unknown after draining again until the failed drain shows it", { timeout: 10_000 }, async (t) => {
  const daemon = await FakeDaemon.start(t);
  const pages: unknown[] = [];
  daemon.route("GET /v1/events/list", async (request) => {
    const after = Number(request.query.get("after") ?? 0);
    if (!request.query.get("wait_millis")) {
      return [200, { events: [], next: 7 }];
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
    return [200, pages.shift() ?? { events: [], next: after }];
  });
  daemon.route("POST /v1/failovers/start", (request) => [
    200,
    {
      name: "app/main.db",
      path: request.body!.path,
      role: "origin",
      database: databaseStatus("app/main.db", "origin", "syncing", 3, 3),
      writer: { name: "app/main.db", active: true, this_machine: true, writer_machine_label: "web-1", lapsed: false },
    },
  ]);
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  let drainFailed = false;
  let roleReads = 0;
  daemon.route("GET /v1/databases/status", () => {
    roleReads++;
    return [200, { ...databaseStatus("app/main.db", "origin", "syncing", 3, 3), draining: !drainFailed }];
  });
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  await litebackup.connect();
  t.after(() => litebackup.close());
  const failover = await litebackup.failovers.start("/var/lib/app/main.db");
  const next = (event: "origin" | "unknown") =>
    new Promise<void>((resolve) => {
      const listener = () => {
        failover.off(event, listener);
        resolve();
      };
      failover.on(event, listener);
    });
  const drained = next("unknown");
  pages.push({ events: [{ id: 8, time: "2026-11-15T09:01:30Z", type: "draining", message: "this machine is draining", name: "app/main.db" }], next: 8 });
  await drained;
  while (roleReads < 2) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(failover.role, "unknown");
  const restored = next("origin");
  drainFailed = true;
  await restored;
  assert.equal(failover.isOrigin, true);
  await failover.stop();
});

test("failover sends discard_local only when discardLocal asks", { timeout: 10_000 }, async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("GET /v1/events/list", async (request) => {
    const after = Number(request.query.get("after") ?? 0);
    if (!request.query.get("wait_millis")) return [200, { events: [], next: 1 }];
    await new Promise((resolve) => setTimeout(resolve, 50));
    return [200, { events: [], next: after }];
  });
  daemon.route("POST /v1/failovers/start", (request) => [
    200,
    {
      name: "app/main.db",
      path: request.body!.path,
      role: "replica",
      database: databaseStatus("app/main.db", "replica", "waiting", 3, 3),
      writer: { name: "app/main.db", active: true, this_machine: false, writer_machine_label: "web-1", lapsed: false },
    },
  ]);
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => litebackup.close());
  const plain = await litebackup.failovers.start("/var/lib/app/main.db");
  await plain.stop();
  const discarding = await litebackup.failovers.start("/var/lib/app/main.db", { discardLocal: true });
  await discarding.stop();
  const bodies = daemon.received("/v1/failovers/start").map((received) => received.body);
  assert.deepEqual(bodies, [{ path: "/var/lib/app/main.db" }, { path: "/var/lib/app/main.db", discard_local: true }]);
});

test("failover listeners hear nothing once stop is called", { timeout: 10_000 }, async (t) => {
  const daemon = await FakeDaemon.start(t);
  const pages: unknown[] = [];
  let waiting: (() => void) | undefined;
  const push = (page: unknown) => {
    pages.push(page);
    waiting?.();
  };
  daemon.route("GET /v1/events/list", async (request) => {
    const after = Number(request.query.get("after") ?? 0);
    if (!request.query.get("wait_millis")) return [200, { events: [], next: 1 }];
    if (pages.length === 0) {
      await new Promise<void>((resolve) => {
        waiting = resolve;
        setTimeout(resolve, 100);
      });
    }
    return [200, pages.shift() ?? { events: [], next: after }];
  });
  daemon.route("POST /v1/failovers/start", (request) => [
    200,
    {
      name: "app/main.db",
      path: request.body!.path,
      role: "replica",
      database: databaseStatus("app/main.db", "replica", "waiting", 3, 3),
      writer: { name: "app/main.db", active: true, this_machine: false, writer_machine_label: "web-1", lapsed: false },
    },
  ]);
  let finishStop: (() => void) | undefined;
  daemon.route("POST /v1/backup-rules/stop", () => new Promise((resolve) => (finishStop = () => resolve([200, {}]))));
  /** Waits for the pending stop request, then answers it. */
  const answerStop = async () => {
    while (!finishStop) await new Promise((resolve) => setTimeout(resolve, 10));
    return finishStop;
  };
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => litebackup.close());
  const roleChanged = (id: number, role: string) => ({
    events: [{ id, time: "2026-11-15T09:01:00Z", type: "role_changed", message: role, name: "app/main.db", role }],
    next: id,
  });

  // A listener that stops the failover keeps the later listeners from hearing the event.
  const first = await litebackup.failovers.start("/var/lib/app/main.db");
  const heard: string[] = [];
  let stopping: Promise<void> | undefined;
  first.on("origin", () => {
    heard.push("stopper");
    stopping = first.stop();
  });
  first.on("origin", () => heard.push("later"));
  push(roleChanged(2, "origin"));
  while (!stopping) await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(heard, ["stopper"]);
  (await answerStop())();
  await stopping;
  finishStop = undefined;

  // While the stop request is pending, events no longer reach listeners.
  const second = await litebackup.failovers.start("/var/lib/app/main.db");
  const late: string[] = [];
  second.on("origin", () => late.push("origin")).on("replica", () => late.push("replica")).on("refused", () => late.push("refused"));
  const stopped = second.stop();
  const finish = await answerStop();
  push(roleChanged(3, "origin"));
  push({ events: [{ id: 4, time: "2026-11-15T09:02:00Z", type: "refused", message: refusedMessage, name: "app/main.db", path: "/var/lib/app/main.db" }], next: 4 });
  await new Promise((resolve) => setTimeout(resolve, 200));
  finish();
  await stopped;
  assert.deepEqual(late, []);
  assert.equal(second.role, "unknown");
  assert.equal(daemon.received("/v1/backup-rules/stop").length, 2);
});

for (const failedRoleReads of [0, 1]) {
  test(`failover resumes at the log's end on an expired cursor and reports the role it read once (failed role reads ${failedRoleReads})`, { timeout: 10_000 }, async (t) => {
    const daemon = await FakeDaemon.start(t);
    const reads: string[] = [];
    let logEndReads = 0;
    let roleReads = 0;
    let freshPages = 0;
    daemon.route("GET /v1/events/list", async (request) => {
      const after = Number(request.query.get("after") ?? 0);
      if (!request.query.get("wait_millis")) {
        logEndReads += 1;
        if (logEndReads === 1) return [200, { events: [], next: 7 }];
        reads.push("log end");
        return [200, { events: [], next: 20 }];
      }
      if (after === 7) return [410, { code: ErrorCode.cursorExpired, message: "events after 7 are no longer kept" }];
      if (after === 20 && freshPages++ === 0) {
        reads.push("after 20");
        return [200, { events: [{ id: 21, time: "2026-11-15T09:01:00Z", type: "role_changed", message: "replica", name: "app/main.db", role: "replica" }], next: 21 }];
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
      return [200, { events: [], next: after }];
    });
    daemon.route("GET /v1/databases/status", (request) => {
      assert.equal(request.query.get("name"), "app/main.db");
      if (roleReads++ < failedRoleReads) {
        reads.push("role failed");
        return [503, { code: specErrorCodes.unavailable, message: "the daemon is busy" }];
      }
      reads.push("role");
      return [200, databaseStatus("app/main.db", "origin", "syncing", 3, 3)];
    });
    daemon.route("POST /v1/failovers/start", (request) => [
      200,
      {
        name: "app/main.db",
        path: request.body!.path,
        role: "replica",
        database: databaseStatus("app/main.db", "replica", "waiting", 3, 3),
        writer: { name: "app/main.db", active: true, this_machine: false, writer_machine_label: "web-1", lapsed: false },
      },
    ]);
    daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
    const litebackup = new Litebackup({ socketPath: daemon.socketPath });
    t.after(() => litebackup.close());

    const failover = await litebackup.failovers.start("/var/lib/app/main.db");
    const heard: string[] = [];
    let resolveBecameReplica: (() => void) | undefined;
    const becameReplica = new Promise<void>((resolve) => (resolveBecameReplica = resolve));
    failover.on("error", (error) => heard.push((error as LitebackupError).code));
    failover.on("origin", () => heard.push("origin"));
    failover.on("replica", () => {
      heard.push("replica");
      // The first is the role the start answered; the second comes from the fresh cursor.
      if (heard.filter((heardEvent) => heardEvent === "replica").length === 2) resolveBecameReplica?.();
    });
    await becameReplica;
    await new Promise((resolve) => setTimeout(resolve, 200));
    const roleReadErrors = failedRoleReads ? [specErrorCodes.unavailable] : [];
    assert.deepEqual(heard, ["replica", ErrorCode.cursorExpired, ...roleReadErrors, "origin", "replica"]);
    // The log's end is read once: a failed role read keeps the cursor it took.
    assert.deepEqual(reads, ["log end", ...(failedRoleReads ? ["role failed"] : []), "role", "log end", "after 20"]);
    await failover.stop();
  });
}

/** A failover start answer with this role. */
function failoverStarted(path: unknown, role: string): [number, unknown] {
  return [
    200,
    {
      name: "app/main.db",
      path,
      role,
      database: databaseStatus("app/main.db", role, role === "origin" ? "syncing" : "waiting", 3, 3),
      writer: { name: "app/main.db", active: true, this_machine: role === "origin", writer_machine_label: "web-1", lapsed: false },
    },
  ];
}

/** Resolves once the listeners reported count times into reported, polling every 10 ms. */
async function awaitReported(reported: unknown[], count: number): Promise<void> {
  while (reported.length < count) await new Promise((resolve) => setTimeout(resolve, 10));
}

test("failover listeners hear the role the start answered once", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("GET /v1/events/list", async (request) => {
    const after = Number(request.query.get("after") ?? 0);
    if (!request.query.get("wait_millis")) return [200, { events: [], next: 7 }];
    await new Promise((resolve) => setTimeout(resolve, 50));
    return [200, { events: [], next: after }];
  });
  daemon.route("POST /v1/failovers/start", (request) => failoverStarted(request.body!.path, "origin"));
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => litebackup.close());
  const failover = await litebackup.failovers.start("/var/lib/app/main.db");
  const heard: string[] = [];
  failover.on("origin", () => heard.push("origin")).on("replica", () => heard.push("replica")).on("unknown", () => heard.push("unknown"));
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.deepEqual(heard, ["origin"]);
  await failover.stop();
  assert.equal(failover.role, "unknown");
  assert.equal(failover.isOrigin, false);
  assert.deepEqual(heard, ["origin"]);
});

for (const refusedStatus of [false, true]) {
  test(`a failover whose daemon stops is unknown at once, and reads its role when the daemon returns (refused status ${refusedStatus})`, { timeout: 10_000 }, async (t) => {
    const daemon = await FakeDaemon.start(t);
    let returned = false;
    const logEndCursors: string[] = [];
    daemon.route("GET /v1/events/list", async (request) => {
      const after = request.query.get("after") ?? "";
      if (!request.query.get("wait_millis")) {
        if (!returned) return [200, { events: [], next: 7 }];
        // The look past the log end after the role read finds nothing new.
        if (after !== "-1") return [200, { events: [], next: Number(after) }];
        logEndCursors.push(after);
        return [200, { events: [], next: 9 }];
      }
      if (returned && after === "7") {
        return [
          200,
          {
            events: [
              { id: 8, time: "2026-11-15T09:01:00Z", type: "role_changed", message: "replica", name: "app/main.db", role: "replica" },
              { id: 9, time: "2026-11-15T09:02:00Z", type: "refused", message: refusedMessage, name: "app/main.db", path: "/var/lib/app/main.db" },
            ],
            next: 9,
          },
        ];
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
      return [200, { events: [], next: Number(after) }];
    });
    // A refused file reads as conflicted, which makes the role unknown though the row says origin.
    daemon.route("GET /v1/databases/status", () => [200, databaseStatus("app/main.db", "origin", refusedStatus ? "conflicted" : "syncing", 3, 3)]);
    daemon.route("POST /v1/failovers/start", (request) => failoverStarted(request.body!.path, "origin"));
    daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
    const litebackup = new Litebackup({ socketPath: daemon.socketPath });
    t.after(() => litebackup.close());
    const failover = await litebackup.failovers.start("/var/lib/app/main.db");
    const roles: string[] = [];
    const errors: string[] = [];
    const refusals: string[] = [];
    failover.on("origin", () => roles.push("origin")).on("replica", () => roles.push("replica")).on("unknown", () => roles.push("unknown"));
    failover.on("refused", (message) => refusals.push(message));
    failover.on("error", (error) => errors.push((error as LitebackupError).code));
    await awaitReported(roles, 1);
    assert.equal(failover.isOrigin, true);

    await daemon.stop();
    const stopped = Date.now();
    await awaitReported(roles, 2);
    assert.ok(Date.now() - stopped < 1000, "the role becomes unknown at once");
    assert.deepEqual(roles, ["origin", "unknown"]);
    assert.equal(failover.role, "unknown");
    assert.equal(failover.isOrigin, false);
    assert.equal(errors[0], ErrorCode.connectionFailed);

    returned = true;
    await daemon.serve();
    await awaitReported(refusals, 1);
    await new Promise((resolve) => setTimeout(resolve, 200));
    // The role read reports origin, or unknown for a refused file; the older role_changed in the kept
    // cursor's page is skipped, and the older refusal is reported without changing the role the read gave.
    assert.deepEqual(roles, refusedStatus ? ["origin", "unknown"] : ["origin", "unknown", "origin"]);
    assert.equal(failover.isOrigin, !refusedStatus);
    // The log's end is read before the role read and again after it; a role left unknown is read again after
    // every long-poll that brings no role change.
    assert.deepEqual(refusedStatus ? logEndCursors.slice(0, 2) : logEndCursors, ["-1", "-1"]);
    assert.deepEqual(refusals, [refusedMessage]);
    await failover.stop();
  });
}

for (const failing of ["answered errors", "no answer"]) {
  test(`a failover role the daemon does not confirm becomes unknown after the confirmation limit (${failing})`, { timeout: 10_000 }, async (t) => {
    const saved = { ...failoverTiming };
    failoverTiming.eventWaitMillis = 100;
    failoverTiming.roleConfirmationLimitMillis = 400;
    t.after(() => Object.assign(failoverTiming, saved));
    const daemon = await FakeDaemon.start(t);
    const hang: (() => void)[] = [];
    t.after(() => hang.forEach((release) => release()));
    daemon.route("GET /v1/events/list", async (request) => {
      if (!request.query.get("wait_millis")) return [200, { events: [], next: 7 }];
      if (failing === "no answer") await new Promise<void>((resolve) => hang.push(resolve));
      return [503, { code: specErrorCodes.unavailable, message: "the daemon is busy" }];
    });
    daemon.route("POST /v1/failovers/start", (request) => failoverStarted(request.body!.path, "origin"));
    daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
    const litebackup = new Litebackup({ socketPath: daemon.socketPath });
    t.after(() => litebackup.close());
    const started = Date.now();
    const failover = await litebackup.failovers.start("/var/lib/app/main.db");
    const roles: string[] = [];
    const errors: string[] = [];
    failover.on("origin", () => roles.push("origin")).on("unknown", () => roles.push("unknown"));
    failover.on("error", (error) => errors.push((error as LitebackupError).code));
    await awaitReported(roles, 2);
    const elapsed = Date.now() - started;
    assert.deepEqual(roles, ["origin", "unknown"]);
    assert.ok(elapsed >= 400 && elapsed < 900, `the role became unknown after ${elapsed} ms, want about 400`);
    assert.equal(failover.isOrigin, false);
    await awaitReported(errors, 1);
    assert.equal(errors[0], failing === "no answer" ? ErrorCode.timeout : specErrorCodes.unavailable);
    await failover.stop();
  });
}

test("a failover reads its role again when the log shows a role change after the role read", { timeout: 10_000 }, async (t) => {
  const daemon = await FakeDaemon.start(t);
  let returned = false;
  let roleReads = 0;
  let logEndReads = 0;
  const racedChanges = {
    events: [
      { id: 10, time: "2026-11-15T09:01:00Z", type: "role_changed", message: "origin", name: "app/main.db", role: "origin" },
      { id: 11, time: "2026-11-15T09:02:00Z", type: "role_changed", message: "replica", name: "app/main.db", role: "replica" },
    ],
    next: 11,
  };
  daemon.route("GET /v1/events/list", async (request) => {
    const after = request.query.get("after") ?? "";
    if (!request.query.get("wait_millis")) {
      if (!returned) return [200, { events: [], next: 7 }];
      // The role changes land after the first log end read.
      if (after === "-1") return [200, { events: [], next: logEndReads++ === 0 ? 9 : 11 }];
      if (after === "9") return [200, racedChanges];
      return [200, { events: [], next: Number(after) }];
    }
    if (returned && after === "7") return [200, racedChanges];
    await new Promise((resolve) => setTimeout(resolve, 50));
    return [200, { events: [], next: Number(after) }];
  });
  daemon.route("GET /v1/databases/status", () => {
    roleReads += 1;
    return [200, databaseStatus("app/main.db", "replica", "waiting", 3, 3)];
  });
  daemon.route("POST /v1/failovers/start", (request) => failoverStarted(request.body!.path, "origin"));
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => litebackup.close());
  const failover = await litebackup.failovers.start("/var/lib/app/main.db");
  const roles: string[] = [];
  failover.on("origin", () => roles.push("origin")).on("replica", () => roles.push("replica")).on("unknown", () => roles.push("unknown"));
  await awaitReported(roles, 1);
  await daemon.stop();
  await awaitReported(roles, 2);
  returned = true;
  await daemon.serve();
  await awaitReported(roles, 3);
  await new Promise((resolve) => setTimeout(resolve, 300));
  // The first role read raced the role changes, so it is read again; the long-poll from the kept cursor reports neither.
  assert.deepEqual(roles, ["origin", "unknown", "replica"]);
  assert.equal(roleReads, 2);
  await failover.stop();
});

test("a failover reads its role again when the log shows a refusal after the role read", { timeout: 10_000 }, async (t) => {
  const daemon = await FakeDaemon.start(t);
  let returned = false;
  let roleReads = 0;
  let logEndReads = 0;
  const racedRefusal = {
    events: [{ id: 10, time: "2026-11-15T09:02:00Z", type: "refused", message: refusedMessage, name: "app/main.db", path: "/var/lib/app/main.db" }],
    next: 10,
  };
  daemon.route("GET /v1/events/list", async (request) => {
    const after = request.query.get("after") ?? "";
    if (!request.query.get("wait_millis")) {
      if (!returned) return [200, { events: [], next: 7 }];
      // The refusal lands after the first log end read.
      if (after === "-1") return [200, { events: [], next: logEndReads++ === 0 ? 9 : 10 }];
      if (after === "9") return [200, racedRefusal];
      return [200, { events: [], next: Number(after) }];
    }
    if (returned && after === "7") return [200, racedRefusal];
    await new Promise((resolve) => setTimeout(resolve, 50));
    return [200, { events: [], next: Number(after) }];
  });
  // A refused file reads as conflicted from the second role read on.
  daemon.route("GET /v1/databases/status", () => {
    roleReads += 1;
    return [200, databaseStatus("app/main.db", "origin", roleReads > 1 ? "conflicted" : "syncing", 3, 3)];
  });
  daemon.route("POST /v1/failovers/start", (request) => failoverStarted(request.body!.path, "origin"));
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => litebackup.close());
  const failover = await litebackup.failovers.start("/var/lib/app/main.db");
  const roles: string[] = [];
  const refusals: string[] = [];
  failover.on("origin", () => roles.push("origin")).on("replica", () => roles.push("replica")).on("unknown", () => roles.push("unknown"));
  failover.on("refused", (message) => refusals.push(message));
  await awaitReported(roles, 1);
  await daemon.stop();
  await awaitReported(roles, 2);
  returned = true;
  await daemon.serve();
  await awaitReported(refusals, 1);
  await new Promise((resolve) => setTimeout(resolve, 300));
  // The first role read raced the refusal, so it is read again: no stale origin, and one refusal. A role
  // left unknown is read again after every long-poll that brings no role change.
  assert.deepEqual(roles, ["origin", "unknown"]);
  assert.deepEqual(refusals, [refusedMessage]);
  assert.ok(roleReads >= 2, `${roleReads} role reads`);
  assert.equal(failover.isOrigin, false);
  await failover.stop();
});

/**
 * A page of events after after, as the daemon pages them: up to 1000, through newest, with next the last ID
 * delivered. All are another database's, except this database's change to origin at 2500 when
 * ownChangeInScan is set, and its change to replica at 2601, which exists only after the last look past the
 * role read stopped at 2600.
 */
function fixedLogEndEvents(after: number, ownChangeInScan: boolean, newest: number): { events: Record<string, unknown>[]; next: number } {
  const events: Record<string, unknown>[] = [];
  for (let id = after + 1; id <= Math.min(after + 1000, newest); id++) {
    const event: Record<string, unknown> = { id, time: "2026-11-15T09:01:00Z", type: "role_changed", message: "origin", name: "other.db", role: "origin" };
    if (id === 2500 && ownChangeInScan) event.name = "app/main.db";
    if (id === 2601) Object.assign(event, { name: "app/main.db", role: "replica", message: "replica" });
    events.push(event);
  }
  return { events, next: (events.at(-1)?.id as number | undefined) ?? after };
}

for (const scanCondition of ["unrelated events", "role change on a later page", "expired cursor"]) {
  test(`a failover's look past the log end after a role read pages through a fixed log end (${scanCondition})`, { timeout: 10_000 }, async (t) => {
    const daemon = await FakeDaemon.start(t);
    const ownChangeInScan = scanCondition === "role change on a later page";
    // The start's log end, the one after the expired cursor, then the end the look past the role read stops at.
    const logEnds = [7, 100];
    let scans = 0;
    let roleReads = 0;
    const wantRoleReads = scanCondition === "unrelated events" ? 1 : 2;
    // The newest event: 2601 once the follower read its last role and the log end after it.
    let newest = 2600;
    daemon.route("GET /v1/events/list", async (request) => {
      const after = Number(request.query.get("after") ?? 0);
      if (request.query.get("wait_millis")) {
        if (after === 7) return [410, { code: ErrorCode.cursorExpired, message: "events after 7 are no longer kept" }];
        const page = fixedLogEndEvents(after, ownChangeInScan, newest);
        if (page.events.length === 0) await new Promise((resolve) => setTimeout(resolve, 50));
        return [200, page];
      }
      if (after === -1) {
        const end = logEnds.shift() ?? newest;
        // This database becomes a replica after the look stopped.
        if (end === newest && roleReads === wantRoleReads) newest = 2601;
        return [200, { events: [], next: end }];
      }
      if (scanCondition === "expired cursor" && scans++ === 0) return [410, { code: ErrorCode.cursorExpired, message: "pruned" }];
      return [200, fixedLogEndEvents(after, ownChangeInScan, newest)];
    });
    daemon.route("GET /v1/databases/status", () => {
      roleReads += 1;
      return [200, databaseStatus("app/main.db", "origin", "syncing", 3, 3)];
    });
    daemon.route("POST /v1/failovers/start", (request) => failoverStarted(request.body!.path, "replica"));
    daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
    const litebackup = new Litebackup({ socketPath: daemon.socketPath });
    t.after(() => litebackup.close());
    const failover = await litebackup.failovers.start("/var/lib/app/main.db");
    const roles: string[] = [];
    failover.on("origin", () => roles.push("origin")).on("replica", () => roles.push("replica"));
    await awaitReported(roles, 3);
    await new Promise((resolve) => setTimeout(resolve, 200));
    // The change to replica after the fixed log end reaches the listeners through the long-poll.
    assert.deepEqual(roles, ["replica", "origin", "replica"]);
    assert.equal(roleReads, wantRoleReads);
    await failover.stop();
  });
}

test("concurrent first calls share a connection and failed connections can retry", async (t) => {
  const daemon = await FakeDaemon.start(t);
  let status = 503;
  daemon.route("GET /v1/status/show", () => [status, {}]);
  const client = new Litebackup({ socketPath: daemon.socketPath });
  await assert.rejects(client.connect(), LitebackupError);
  status = 200;
  await Promise.all([client.connect(), client.connect()]);
  assert.equal(daemon.received("/v1/status/show").length, 2);
  await assert.rejects(client.backups.once("main.db"), { code: "invalid_argument" });
  client.close();
  await assert.rejects(client.connect(), { code: "closed" });
});

test("a caller's signal and close() end a wait on a daemon that never answers the connect probe", { timeout: 10_000 }, async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("GET /v1/status/show", () => new Promise(() => {}));
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  t.after(() => process.off("unhandledRejection", onUnhandled));
  const client = new Litebackup({ socketPath: daemon.socketPath });

  const controller = new AbortController();
  const started = Date.now();
  const cancelled = assert.rejects(client.databases.get("app.db", { signal: controller.signal }), { code: "cancelled" });
  const waiting = assert.rejects(client.connect(), { code: "closed" });
  setTimeout(() => controller.abort(), 20);
  await cancelled;
  assert.ok(Date.now() - started < 5_000, "the cancelled caller stops waiting promptly");

  // The other caller still waits on the shared attempt until the client closes.
  client.close();
  await waiting;
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(unhandled, []);
  assert.equal(daemon.received("/v1/status/show").length, 1);
  assert.equal(daemon.received("/v1/databases/status").length, 0);
});

test("resolves the service socket where litebackup install service ran", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "litebackup-sdk-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const configurationPath = join(directory, "client.json");
  const environment = { XDG_RUNTIME_DIR: join(directory, "home"), HOME: join(directory, "user") };
  assert.equal(sessionSocket(undefined, environment, configurationPath).socketPath, join(directory, "home", "litebackup", "default", "client.sock"));
  await writeFile(configurationPath, JSON.stringify({ user: "app", uid: userInfo().uid, state_directory: "/var/lib/litebackup-client", runtime_directory: join(directory, "run") }));
  assert.equal(sessionSocket("reports", environment, configurationPath).socketPath, join(directory, "run", "reports", "client.sock"));
  assert.throws(() => sessionSocket("Bad Name", environment, configurationPath));
});

test("resolves a user service socket from its service file, whatever the environment", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "litebackup-sdk-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const serviceFile = join(directory, ".config", "litebackup", "service.json");
  await mkdir(dirname(serviceFile), { recursive: true });
  await writeFile(serviceFile, JSON.stringify({ session: "default", state_directory: "/var/lib/app/state/litebackup", runtime_directory: "/run/user/1000/litebackup" }));
  const environment = { HOME: directory };
  const configurationPath = join(directory, "client.json");
  assert.equal(sessionSocket(undefined, environment, configurationPath).socketPath, "/run/user/1000/litebackup/default/client.sock");
  for (const name of ["..", ".", ".hidden"]) {
    assert.throws(() => sessionSocket(name, environment, configurationPath));
  }
});

test("names the install command when no binary is found, and finds a user install", async (t) => {
  const home = await mkdtemp(join(tmpdir(), "litebackup-home-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const override = join(home, "litebackup");
  await writeFile(override, "#!/bin/sh\n", { mode: 0o755 });
  assert.equal(litebackupBinary({ LITEBACKUP_PATH: override }), override);
  // When this platform's optional binary package is installed, it comes before
  // PATH and ~/.local/bin.
  const packageBinary = platformBinary();
  if (packageBinary) {
    assert.equal(litebackupBinary({ PATH: "/nonexistent", HOME: home }), packageBinary);
  } else if (process.platform === "linux" || process.platform === "darwin") {
    assert.throws(() => litebackupBinary({ PATH: "/nonexistent", HOME: home }), (error: unknown) => error instanceof Error && error.message.includes(installCommand()));
  }
  assert.equal(installCommand(), "curl -fsSL https://litebackup.com/cli.sh | bash");
  const local = join(home, ".local", "bin", "litebackup");
  await mkdir(dirname(local), { recursive: true });
  await writeFile(local, "#!/bin/sh\n", { mode: 0o755 });
  if (process.platform === "linux" || process.platform === "darwin") {
    assert.equal(litebackupBinary({ PATH: "/nonexistent", HOME: home }), packageBinary ?? local);
  }
});

test("prefix replicas register a directory, preserve an empty prefix, and stop by prefix and directory", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("POST /v1/replicas/start", (request) => [200, { prefix_replica: { id: "p1", prefix: request.body!.prefix, directory: request.body!.directory, write_protection: "lock", database_count: 0 } }]);
  daemon.route("POST /v1/replicas/stop", () => [200, {}]);
  const client = new Litebackup({ socketPath: daemon.socketPath });
  await client.connect();
  const prefix = await client.replicas.startPrefix("tenants", { directory: "replicas", writeProtection: "lock" });
  assert.equal(prefix.id, "p1");
  assert.equal(prefix.writeProtection, "lock");
  assert.deepEqual(daemon.received("/v1/replicas/start")[0]!.body, { prefix: "tenants", directory: join(process.cwd(), "replicas"), write_protection: "lock" });
  await client.replicas.stopPrefix("tenants", "replicas");
  assert.deepEqual(daemon.received("/v1/replicas/stop")[0]!.body, { prefix: "tenants", directory: join(process.cwd(), "replicas") });
  await client.replicas.startPrefix("", { directory: "all" });
  assert.equal(daemon.received("/v1/replicas/start")[1]!.body!.prefix, "");
  await assert.rejects(client.replicas.startPrefix("tenants", { directory: "" }), (error: unknown) => error instanceof LitebackupError && error.code === "invalid_argument");
  await assert.rejects(client.replicas.stopPrefix("tenants", ""), LitebackupError);
  daemon.route("POST /v1/replicas/start", () => [200, {}]);
  await assert.rejects(client.replicas.startPrefix("tenants", { directory: "replicas" }), (error: unknown) => error instanceof LitebackupError && error.code === "invalid_response");
  daemon.route("POST /v1/replicas/start", () => [403, { code: "forbidden", message: "namespace not allowed" }]);
  await assert.rejects(client.replicas.startPrefix("tenants", { directory: "replicas" }), (error: unknown) => error instanceof LitebackupError && error.code === "forbidden");
});

test("foreground backup rejects cancellation before launch", async () => {
  await assert.rejects(new Litebackup().backups.once("main.db", { signal: AbortSignal.abort() }), (e: unknown) => e instanceof LitebackupError && e.code === "cancelled");
});

test("closing the client during a foreground backup reports closed, not cancelled", { timeout: 10_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-foreground-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binaryPath = join(directory, "litebackup");
  await writeFile(binaryPath, "#!/bin/sh\nexec sleep 30\n", { mode: 0o755 });
  for (const signal of [undefined, new AbortController().signal]) {
    const client = new Litebackup({ binaryPath, session: "unit", environment: { PATH: process.env.PATH } });
    const running = client.backups.once(join(directory, "main.db"), { signal });
    setTimeout(() => client.close(), 50);
    await assert.rejects(running, { code: "closed" });
  }
  const client = new Litebackup({ binaryPath, session: "unit", environment: { PATH: process.env.PATH } });
  const controller = new AbortController();
  const running = client.backups.once(join(directory, "main.db"), { signal: controller.signal });
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(running, { code: "cancelled" });
});

test("credentials other than an API key or a join token are rejected without echoing them", async () => {
  const invite = "litebackup_invite_exampleinvitevalue@backups.example.com";
  for (const client of [new Litebackup({ apiKey: invite }), new Litebackup({ environment: { [apiKeyEnvironment]: invite } })]) {
    for (const call of [() => client.connect(), () => client.backups.once("main.sqlite")]) {
      await assert.rejects(call(), (err: unknown) => err instanceof LitebackupError &&
        err.code === "invalid_argument" && err.message.includes("invite token") && !err.message.includes("exampleinvitevalue"));
    }
  }
});

/** A litebackup stand-in that appends its command and the LITEBACKUP_API_KEY it got ("unset" without one) to a file. */
async function credentialStandIn(t: { after: (fn: () => Promise<void>) => void }): Promise<{ binaryPath: string; calls: () => Promise<string[]>; environment: NodeJS.ProcessEnv }> {
  const directory = await mkdtemp(join(tmpdir(), "lb-credential-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binaryPath = join(directory, "litebackup");
  const callsFile = join(directory, "calls");
  await writeFile(binaryPath, `#!/bin/sh\nprintf '%s %s\\n' "$1" "\${LITEBACKUP_API_KEY-unset}" >> '${callsFile}'\n`, { mode: 0o755 });
  const calls = async () => existsSync(callsFile) ? (await readFile(callsFile, "utf8")).replace(/\n$/, "").split("\n") : [];
  return { binaryPath, calls, environment: { PATH: process.env.PATH, HOME: directory, XDG_RUNTIME_DIR: directory } };
}

test("launch gets the apiKey option over LITEBACKUP_API_KEY, the variable without it, and nothing when neither is set", async (t) => {
  const joinToken = "litebackup_join_exampletokenvalue@backups.example.com";
  const apiKey = "litebackup_examplekeyvalue@backups.example.com";
  const connects: { variable?: string; option?: string; launched: string }[] = [
    { option: joinToken, launched: `launch ${joinToken}` },
    { variable: joinToken, option: apiKey, launched: `launch ${apiKey}` },
    { variable: apiKey, launched: `launch ${apiKey}` },
    { variable: joinToken, launched: `launch ${joinToken}` },
    // The daemon then uses the key it saved.
    { launched: "launch unset" },
    // An empty apiKey is no credential, whatever the variable holds.
    { variable: joinToken, option: "", launched: "launch " },
  ];
  for (const connect of connects) {
    const standIn = await credentialStandIn(t);
    const environment = { ...standIn.environment };
    if (connect.variable !== undefined) environment[apiKeyEnvironment] = connect.variable;
    const client = new Litebackup({ binaryPath: standIn.binaryPath, session: "unit", environment, apiKey: connect.option });
    await assert.rejects(client.connect());
    client.close();
    assert.deepEqual(await standIn.calls(), [connect.launched]);
  }
});

test("a foreground backup gets the apiKey option", async (t) => {
  const standIn = await credentialStandIn(t);
  const apiKey = "litebackup_examplekeyvalue@backups.example.com";
  const environment = { ...standIn.environment, [apiKeyEnvironment]: "litebackup_join_exampletokenvalue@backups.example.com" };
  const client = new Litebackup({ binaryPath: standIn.binaryPath, session: "unit", environment, apiKey });
  await client.backups.once("/var/lib/app/main.sqlite").catch(() => undefined);
  client.close();
  assert.deepEqual(await standIn.calls(), [`backup ${apiKey}`]);
});

/** The binary of this platform's optional npm package, when it is installed. */
function platformBinary(): string | undefined {
  try {
    const require = createRequire(import.meta.url);
    const binaryPath = join(dirname(require.resolve(`@litebackup/bin-${process.platform}-${process.arch}/package.json`)), "bin", "litebackup");
    return existsSync(binaryPath) ? binaryPath : undefined;
  } catch {
    return undefined;
  }
}

test("page limits outside 1 to 1000 are refused before sending", async (t) => {
  const daemon = await FakeDaemon.start(t);
  const client = new Litebackup({ socketPath: daemon.socketPath });
  t.after(() => client.close());
  for (const limit of [0, -1, 1001, 1.5]) {
    await assert.rejects(client.session.status({ limit }), (e: unknown) => e instanceof LitebackupError && e.code === "invalid_argument", `status ${limit}`);
    await assert.rejects(client.databases.page({ limit }), (e: unknown) => e instanceof LitebackupError && e.code === "invalid_argument", `page ${limit}`);
  }
  assert.equal(daemon.received("/v1/databases/list").length, 0);
});

test("a foreground backup with a missing binary is binary_not_found", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-missing-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const client = new Litebackup({ binaryPath: join(directory, "absent"), session: "unit", environment: { PATH: process.env.PATH } });
  t.after(() => client.close());
  await assert.rejects(client.backups.once(join(directory, "a.db")), (e: unknown) => e instanceof LitebackupError && e.code === "binary_not_found");
});

test("a foreground backup that outlives its timeout and grace is stopped and rejects with timeout", { timeout: 10_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-deadline-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const wedged = join(directory, "wedged");
  // Ignores --timeout and SIGTERM, so only SIGKILL ends it.
  await writeFile(wedged, `#!${process.execPath}\nprocess.on("SIGTERM", () => {});\nsetTimeout(() => {}, 30_000);\n`, { mode: 0o755 });
  const timing = { graceMillis: 100, killAfterMillis: 100 };
  const started = Date.now();
  await assert.rejects(foregroundBackup(join(directory, "main.db"), { timeout: 50 }, { PATH: process.env.PATH }, "unit", wedged, timing),
    (e: unknown) => e instanceof LitebackupError && e.code === "timeout");
  assert.ok(Date.now() - started < 5_000);

  // The CLI's own timeout answer wins when it arrives before the deadline.
  const answering = join(directory, "answering");
  await writeFile(answering, "#!/bin/sh\necho '{\"code\":\"timeout\",\"message\":\"the job timed out after 50ms\"}' >&2\nexit 1\n", { mode: 0o755 });
  await assert.rejects(foregroundBackup(join(directory, "main.db"), { timeout: 50 }, { PATH: process.env.PATH }, "unit", answering, timing),
    (e: unknown) => e instanceof LitebackupError && e.code === "timeout" && e.message.includes("the job timed out after 50ms"));
});

test("a stopped foreground backup settles even when a process it started keeps its output open", { timeout: 10_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-deadline-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const wedged = join(directory, "wedged");
  // Starts a process that inherits stderr and outlives it, then ignores SIGTERM.
  await writeFile(wedged, "#!/bin/sh\nsleep 30 >&2 &\ntrap '' TERM\nwhile :; do sleep 0.05; done\n", { mode: 0o755 });
  const started = Date.now();
  await assert.rejects(foregroundBackup(join(directory, "main.db"), { timeout: 50 }, { PATH: process.env.PATH }, "unit", wedged, { graceMillis: 100, killAfterMillis: 100 }),
    (e: unknown) => e instanceof LitebackupError && e.code === "timeout");
  assert.ok(Date.now() - started < 5_000);
});

test("a foreground backup settles when the CLI exits while a process it started keeps its output open", { timeout: 10_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-deadline-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const early = join(directory, "early");
  await writeFile(early, "#!/bin/sh\nsleep 30 >&2 &\nexit 0\n", { mode: 0o755 });
  const started = Date.now();
  await foregroundBackup(join(directory, "main.db"), { timeout: 50 }, { PATH: process.env.PATH }, "unit", early, { graceMillis: 100, killAfterMillis: 100, drainMillis: 100 });
  assert.ok(Date.now() - started < 5_000);
});

test("a foreground backup stopped at its deadline fails even when the CLI exits cleanly on SIGTERM", { timeout: 10_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-deadline-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const polite = join(directory, "polite");
  await writeFile(polite, "#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do sleep 0.05; done\n", { mode: 0o755 });
  await assert.rejects(foregroundBackup(join(directory, "main.db"), { timeout: 50 }, { PATH: process.env.PATH }, "unit", polite, { graceMillis: 100, killAfterMillis: 100 }),
    (e: unknown) => e instanceof LitebackupError && e.code === "timeout");
});

test("a foreground backup timeout past the timer limit does not fire at once", { timeout: 10_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-deadline-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const slow = join(directory, "slow");
  await writeFile(slow, "#!/bin/sh\nsleep 0.3\nexit 0\n", { mode: 0o755 });
  await foregroundBackup(join(directory, "main.db"), { timeout: "600h" }, { PATH: process.env.PATH }, "unit", slow);
});

test("a failed foreground backup keeps its first diagnostic after long output", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-diagnostic-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const noisy = join(directory, "noisy");
  await writeFile(
    noisy,
    "#!/bin/sh\n" +
      "echo '{\"code\":\"invalid_database\",\"message\":\"broken.db: not a database\"}' >&2\n" +
      "head -c 70000 /dev/zero | tr '\\0' 'x' >&2\necho >&2\n" +
      "echo '{\"code\":\"backup_failed\",\"message\":\"one file failed\"}' >&2\nexit 1\n",
    { mode: 0o755 },
  );
  await assert.rejects(foregroundBackup(join(directory, "main.db"), {}, { PATH: process.env.PATH }, "unit", noisy),
    (e: unknown) => e instanceof LitebackupError && e.code === "backup_failed" && e.message.includes("broken.db: not a database"));
});

test("a failed foreground backup lists every file diagnostic, however much output lies between", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-diagnostics-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const noisy = join(directory, "noisy");
  const logs = "head -c 70000 /dev/zero | tr '\\0' 'x' >&2\necho >&2\n";
  await writeFile(
    noisy,
    "#!/bin/sh\n" + logs +
      "echo '{\"code\":\"busy\",\"message\":\"/var/lib/app/a.sqlite: another session writes this database (host-b)\"}' >&2\n" +
      logs + logs +
      "echo '{\"code\":\"invalid_database\",\"message\":\"/var/lib/app/b.sqlite: not a database\"}' >&2\n" +
      logs +
      "echo '{\"code\":\"busy\",\"message\":\"2 files could not be backed up\"}' >&2\nexit 1\n",
    { mode: 0o755 },
  );
  await assert.rejects(foregroundBackup(join(directory, "main.db"), {}, { PATH: process.env.PATH }, "unit", noisy), (e: unknown) => {
    assert.ok(e instanceof LitebackupError);
    assert.equal(e.code, ErrorCode.busy);
    assert.equal(
      e.message,
      "busy: 2 files could not be backed up\n" +
        "/var/lib/app/a.sqlite: another session writes this database (host-b)\n" +
        "/var/lib/app/b.sqlite: not a database",
    );
    return true;
  });
});

test("a failed foreground backup counts each file once toward its diagnostic limit", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-repeated-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const repeating = join(directory, "repeating");
  await writeFile(
    repeating,
    "#!/bin/sh\n" +
      "for n in $(seq 25); do echo '{\"code\":\"busy\",\"message\":\"/var/lib/app/a.sqlite: another session writes this database (host-b)\"}' >&2; done\n" +
      "echo '{\"code\":\"invalid_database\",\"message\":\"/var/lib/app/b.sqlite: not a database\"}' >&2\n" +
      "echo '{\"code\":\"busy\",\"message\":\"2 files could not be backed up\"}' >&2\nexit 1\n",
    { mode: 0o755 },
  );
  await assert.rejects(foregroundBackup(join(directory, "main.db"), {}, { PATH: process.env.PATH }, "unit", repeating), (e: unknown) => {
    assert.ok(e instanceof LitebackupError);
    assert.equal(
      e.message,
      "busy: 2 files could not be backed up\n" +
        "/var/lib/app/a.sqlite: another session writes this database (host-b)\n" +
        "/var/lib/app/b.sqlite: not a database",
    );
    return true;
  });
});

test("the packages carry their license and where to find the source", async () => {
  const { readFile, readdir } = await import("node:fs/promises");
  const root = new URL("../../", import.meta.url);
  const sdk = JSON.parse(await readFile(new URL("package.json", root), "utf8"));
  assert.equal(sdk.license, "MIT");
  assert.deepEqual(sdk.repository, { type: "git", url: "git+https://github.com/litebackup/sdk.git", directory: "typescript" });
  assert.deepEqual(sdk.bugs, { url: "https://github.com/litebackup/sdk/issues" });
  assert.equal(sdk.homepage, "https://litebackup.com/sdk");
  for (const platform of await readdir(new URL("npm/", root))) {
    const binary = JSON.parse(await readFile(new URL(`npm/${platform}/package.json`, root), "utf8"));
    assert.equal(binary.license, "MIT", platform);
  }
});

test("a timeout longer than Node's timers waits for the daemon's answer", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("POST /v1/databases/acquire", async () => {
    await new Promise((resolve) => setTimeout(resolve, 100));
    return [200, { database: databaseStatus("tenants/42.db", "origin", "syncing", 0, 0), decision: "created" }];
  });
  const litebackup = new Litebackup({ socketPath: daemon.socketPath });
  await litebackup.connect();
  const acquired = await litebackup.databases.acquire("tenants/42.db", { path: "/var/lib/app/42.db", timeout: "720h" });
  assert.equal(acquired.decision, "created");
  assert.equal(daemon.received("/v1/databases/acquire")[0]!.body!.timeout_millis, 720 * 3600 * 1000);
});

test("refuses a shared runtime directory when XDG_RUNTIME_DIR is unset", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "litebackup-sdk-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const configurationPath = join(directory, "client.json");
  const environment = { TMPDIR: directory, HOME: join(directory, "user") };
  const runtimeRoot = join(directory, `litebackup-${userInfo().uid}`);
  assert.equal(sessionSocket(undefined, environment, configurationPath).socketPath, join(runtimeRoot, "litebackup", "default", "client.sock"));
  assert.equal(lstatSync(runtimeRoot).mode & 0o777, 0o700);
  await chmod(runtimeRoot, 0o777);
  assert.throws(() => sessionSocket(undefined, environment, configurationPath), (error: unknown) => error instanceof LitebackupError && error.code === "invalid_argument");
});

test("a cancelled connect stops waiting at once, and close() ends the launch command", { timeout: 15_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "litebackup-sdk-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const pidFile = join(directory, "launch.pid");
  const binary = join(directory, "litebackup");
  await writeFile(binary, `#!/bin/sh\necho $$ > "${pidFile}"\nexec sleep 30\n`, { mode: 0o755 });
  const runtime = join(directory, "run");
  await mkdir(runtime, { mode: 0o700 });
  const client = new Litebackup({ session: "slowlaunch", binaryPath: binary, environment: { HOME: directory, XDG_RUNTIME_DIR: runtime, PATH: process.env.PATH } });
  const controller = new AbortController();
  const started = Date.now();
  const connecting = assert.rejects(client.connect({ signal: controller.signal }), { code: "cancelled" });
  while (!existsSync(pidFile)) await new Promise((resolve) => setTimeout(resolve, 10));
  controller.abort();
  await connecting;
  assert.ok(Date.now() - started < 5_000, "the cancelled connect stops waiting promptly");
  const pid = Number((await readFile(pidFile, "utf8")).trim());
  await client.close();
  const deadline = Date.now() + 5_000;
  let running = true;
  while (running && Date.now() < deadline) {
    try {
      process.kill(pid, 0);
      await new Promise((resolve) => setTimeout(resolve, 20));
    } catch {
      running = false;
    }
  }
  assert.equal(running, false, "close() leaves the launch command running");
});

test("launch_failed carries the launch command's own message, which it asks for as JSON", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-launch-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binaryPath = join(directory, "litebackup");
  const argumentsFile = join(directory, "arguments");
  await writeFile(binaryPath, [
    "#!/bin/sh",
    `echo "$@" > '${argumentsFile}'`,
    `echo '{"event":"warning","code":"${specErrorCodes.notConfigured}","message":"the server waits for setup"}' >&2`,
    `echo '{"code":"${specErrorCodes.unauthenticated}","message":"this machine has not joined a server"}' >&2`,
    "exit 1",
    "",
  ].join("\n"), { mode: 0o755 });
  const client = new Litebackup({ binaryPath, session: "unit", environment: { PATH: process.env.PATH, HOME: directory, XDG_RUNTIME_DIR: directory } });
  await assert.rejects(client.connect(), (err: unknown) => err instanceof LitebackupError && err.code === ErrorCode.launchFailed &&
    err.message === "launch_failed: starting Litebackup failed: this machine has not joined a server");
  client.close();
  assert.equal(await readFile(argumentsFile, "utf8"), "launch --session unit --stdout-format json\n");
  assert.deepEqual(launchRefusal("plain text\n"), { code: ErrorCode.launchFailed, message: "plain text" });
});

test("a launch that refuses another credential while locks are taken keeps drain_required", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-drain-required-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binaryPath = join(directory, "litebackup");
  await writeFile(binaryPath, [
    "#!/bin/sh",
    `echo '{"code":"${specErrorCodes.drainRequired}","message":"drain it first with litebackup drain, then change the key"}' >&2`,
    "exit 1",
    "",
  ].join("\n"), { mode: 0o755 });
  const client = new Litebackup({ binaryPath, session: "unit", environment: { PATH: process.env.PATH, HOME: directory, XDG_RUNTIME_DIR: directory } });
  await assert.rejects(client.connect(), (err: unknown) => err instanceof LitebackupError && err.code === ErrorCode.drainRequired &&
    err.message === "drain_required: starting Litebackup failed: drain it first with litebackup drain, then change the key");
  client.close();
});

test("a binary that is there but cannot run names why, at launch and in its message", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "lb-interpreter-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binaryPath = join(directory, "litebackup");
  await writeFile(binaryPath, "#!/nonexistent/litebackup-interpreter\n", { mode: 0o755 });
  const client = new Litebackup({ binaryPath, session: "unit", environment: { PATH: process.env.PATH, HOME: directory, XDG_RUNTIME_DIR: directory } });
  await assert.rejects(client.connect(), (err: unknown) => err instanceof LitebackupError && err.code === ErrorCode.launchFailed &&
    err.message.includes(`starting ${binaryPath} failed: it cannot run because the interpreter its first line names is not on this machine: #!/nonexistent/litebackup-interpreter`));
  client.close();
  const missing = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
  const notDirectory = Object.assign(new Error("spawn ENOTDIR"), { code: "ENOTDIR" });
  assert.equal(startFailureMessage(binaryPath, notDirectory),
    `starting ${binaryPath} failed: it cannot run because the interpreter its first line names is not on this machine: #!/nonexistent/litebackup-interpreter`);
  const loaderMissing = join(directory, "native");
  await writeFile(loaderMissing, "\x7fELF", { mode: 0o755 });
  assert.equal(startFailureMessage(loaderMissing, missing),
    `starting ${loaderMissing} failed: it cannot run because its interpreter or dynamic loader is not on this machine`);
  const absent = join(directory, "absent");
  assert.equal(startFailureMessage(absent, missing), `starting ${absent} failed: spawn ENOENT`);
});
