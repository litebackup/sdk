import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir, userInfo } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";

import { Litebackup, LitebackupError, backup, startBackup, installCommand, litebackupBinary, sdkVersion, sessionSocket, versions } from "../src/index.js";
import { FakeDaemon, databaseStatus } from "./fake.js";

test("sends the SDK version, converts answers to camelCase, and maps errors", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("GET /v1/databases/status", (request) => [200, databaseStatus(request.query.get("name")!, "origin", "syncing", 3, 2)]);
  daemon.route("POST /v1/databases/release", () => [409, { code: "still_open", message: "a process still has the database open" }]);
  daemon.route("POST /v1/databases/offload", () => [426, "not json"]);
  const litebackup = await Litebackup.connect({ socketPath: daemon.socketPath });
  assert.equal(litebackup.clientVersion, "0.3.18");

  const status = await litebackup.database("app/main.db");
  assert.equal(status.syncedServer.version, 3);
  assert.equal(status.syncedCloud.version, 2);
  assert.equal(status.lastFullScanReason, "first");
  assert.equal(status.localChanged, false);
  assert.equal((status as unknown as Record<string, unknown>).synced_server, undefined);

  await assert.rejects(litebackup.release("app/main.db", { wait: "cloud", timeout: "5s" }), (error: unknown) => {
    assert.ok(error instanceof LitebackupError);
    assert.equal(error.code, "still_open");
    assert.equal(error.status, 409);
    return true;
  });
  assert.deepEqual(daemon.received("/v1/databases/release")[0]!.body, { name: "app/main.db", wait: "cloud", timeout_millis: 5000 });
  await assert.rejects(litebackup.offload("x.db"), (error: unknown) => error instanceof LitebackupError && error.code === "http_426");
  for (const request of daemon.requests) {
    assert.equal(request.headers["x-litebackup-sdk-version"], `typescript/${sdkVersion}`);
  }
  assert.equal(sdkVersion, "0.4.0");
});

test("startBackup awaits the local rule and stop reports a pending lease", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("POST /v1/backup-rules/start", (req) => [200, { id: "rule", path: req.body!.path }]);
  daemon.route("POST /v1/backup-rules/stop", () => [200, { lease_pending: true }]);
  const client = await Litebackup.connect({ socketPath: daemon.socketPath });
  const source = { name: "My App.db", open: true, memory: false };
  assert.equal((await client.startBackup(source, { prefix: "É/" })).id, "rule");
  assert.deepEqual(daemon.received("/v1/backup-rules/start")[0]!.body, { path: join(process.cwd(), source.name), prefix: "É/" });
  assert.deepEqual(await client.stopBackup("É/My App.db"), { leasePending: true });
  assert.deepEqual(await client.stopBackupRule("rule"), { leasePending: true });
  assert.deepEqual(
    daemon.received("/v1/backup-rules/stop").map((req) => req.body),
    [{ name: "É/My App.db" }, { rule_id: "rule" }],
  );
  await assert.rejects(client.startBackup({ ...source, memory: true }), LitebackupError);
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
  daemon.route("POST /v1/databases/acquire", () => [200, { database: databaseStatus("tenants/42.db", "origin", "syncing", 0, 0), outcome: "created" }]);
  daemon.route("GET /v1/databases/writer", () => [200, { name: "tenants/42.db", active: true, this_machine: true, machine: "web-1", lease_remaining_millis: 5000, lapsed: false }]);
  const litebackup = await Litebackup.connect({ socketPath: daemon.socketPath });

  const listed = await litebackup.versions("app/main.db");
  assert.deepEqual(listed, [
    { version: 1, stateDigest: "aa", time: "2026-11-15T09:30:00Z" },
    { version: 2, stateDigest: "bb", time: "2026-11-15T09:31:00Z" },
  ]);
  assert.equal(daemon.received("/v1/databases/versions")[1]!.query.get("after"), "1");

  const restored = await litebackup.restore("app/main.db", { to: "/tmp/restored.db", time: new Date("2026-11-15T09:30:30Z") });
  assert.equal(restored.pageSize, 4096);
  assert.deepEqual(daemon.received("/v1/databases/restore")[0]!.body, { name: "app/main.db", path: "/tmp/restored.db", time: "2026-11-15T09:30:30.000Z" });
  assert.throws(() => litebackup.restore("app/main.db", { to: "/tmp/x.db", version: 1, time: "2026-11-15T09:30:30Z" }), LitebackupError);

  const replica = await litebackup.replicate("app/main.db", { to: "replica/main.db", writeProtection: "detect", timeout: "2m" });
  assert.equal(replica.role, "replica");
  assert.deepEqual(daemon.received("/v1/replicas/start")[0]!.body, { name: "app/main.db", to: join(process.cwd(), "replica", "main.db"), write_protection: "detect", timeout_millis: 120000 });

  const acquired = await litebackup.acquire("tenants/42.db", { to: "/var/lib/app/42.db", offload: "released", machineLabel: "worker-3", onLost: "block" });
  assert.equal(acquired.outcome, "created");
  assert.deepEqual(daemon.received("/v1/databases/acquire")[0]!.body, { name: "tenants/42.db", path: "/var/lib/app/42.db", offload: "released", machine_label: "worker-3", on_lost: "block" });
  const writer = await litebackup.writer("tenants/42.db");
  assert.equal(writer.thisMachine, true);
  assert.equal(writer.leaseRemainingMillis, 5000);
});

test("failover follows role_changed and conflict_saved events", async (t) => {
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
      writer: { name: "app/main.db", active: true, this_machine: false, machine: "web-1", lapsed: false },
    },
  ]);
  daemon.route("POST /v1/backup-rules/stop", () => [200, {}]);
  const litebackup = await Litebackup.connect({ socketPath: daemon.socketPath });
  t.after(() => litebackup.close());

  const failover = await litebackup.failover("/var/lib/app/main.db");
  assert.deepEqual(daemon.received("/v1/failovers/start")[0]!.body, { path: "/var/lib/app/main.db" });
  assert.equal(failover.role, "replica");
  assert.equal(failover.isOrigin, false);
  assert.equal(failover.info.writer.machine, "web-1");

  const seen: string[] = [];
  const next = (event: "origin" | "replica" | "conflictSaved") =>
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

  const becameReplica = next("replica");
  const saved = next("conflictSaved");
  push({
    events: [
      { id: 10, time: "2026-11-15T09:02:00Z", type: "role_changed", message: "replica", name: "app/main.db", role: "replica" },
      { id: 11, time: "2026-11-15T09:02:00Z", type: "conflict_saved", message: "/var/lib/app/main.db.conflict-1.sqlite", name: "app/main.db", path: "/var/lib/app/main.db.conflict-1.sqlite" },
    ],
    next: 11,
  });
  await becameReplica;
  assert.equal(await saved, "/var/lib/app/main.db.conflict-1.sqlite");
  for (const request of daemon.received("/v1/events/list")) {
    if (request.query.get("wait_millis")) {
      assert.ok(Number(request.query.get("after")) >= 7, "the long-poll starts at the end of the log");
    }
  }

  assert.deepEqual(seen, ["origin", "replica"]);
  await failover.stop();
  assert.deepEqual(daemon.received("/v1/backup-rules/stop")[0]!.body, { name: "app/main.db" });
  assert.equal(litebackup.failovers.size, 0, "stopping the failover ends its long-poll");
});

test("the package helpers share one client per token and session", async (t) => {
  const daemon = await FakeDaemon.start(t, "helpers");
  daemon.route("GET /v1/databases/versions", () => [200, { versions: [{ version: 1, state_digest: "aa", time: "2026-11-15T09:30:00Z" }] }]);
  daemon.route("POST /v1/backup-rules/start", (request) => [
    200,
    { id: "b3", path: request.body!.path, name: "", prefix: "", watch: "inotify", maximum_local_databases: 0, failover: false, databases: [databaseStatus("main.db", "origin", "syncing", 1, 0)] },
  ]);
  const saved = { ...process.env };
  t.after(() => {
    process.env = saved;
  });
  process.env.XDG_RUNTIME_DIR = daemon.directory;
  process.env.LITEBACKUP_SESSION = "helpers";
  process.env.LITEBACKUP_TOKEN = "token@http://example";
  assert.equal((await versions("main.db")).length, 1);
  assert.equal((await versions("main.db", { token: "token@http://example" })).length, 1);
  assert.equal((await startBackup("/var/lib/app/main.db")).id, "b3");
  assert.equal(daemon.received("/v1/status/show").length, 1, "one client served every helper call");
  await versions("main.db", { token: "other@http://example" });
  assert.equal(daemon.received("/v1/status/show").length, 2, "another token gets its own client");
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

test("resolves a user service socket from its record, whatever the environment", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "litebackup-sdk-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const record = join(directory, ".config", "litebackup", "service.json");
  await mkdir(dirname(record), { recursive: true });
  await writeFile(record, JSON.stringify({ session: "default", state_directory: "/var/lib/app/state/litebackup", runtime_directory: "/run/user/1000/litebackup" }));
  const environment = { HOME: directory };
  const configurationPath = join(directory, "client.json");
  assert.equal(sessionSocket(undefined, environment, configurationPath).socketPath, "/run/user/1000/litebackup/default/client.sock");
  for (const name of ["..", ".", ".hidden"]) {
    assert.throws(() => sessionSocket(name, environment, configurationPath));
  }
});

test("names the install command when no binary is found", () => {
  assert.equal(litebackupBinary({ LITEBACKUP_PATH: "/opt/litebackup" }), "/opt/litebackup");
  if (process.platform === "linux" || process.platform === "darwin") {
    // The platform package is not installed in a development tree.
    assert.throws(() => litebackupBinary({ PATH: "/nonexistent" }), (error: unknown) => error instanceof Error && error.message.includes(installCommand()));
  }
  assert.equal(installCommand(), "curl -fsSL https://litebackup.com/cli.sh | sudo bash");
});

test("prefix replicas register a directory, preserve an empty prefix, and stop by prefix and directory", async (t) => {
  const daemon = await FakeDaemon.start(t);
  daemon.route("POST /v1/replicas/start", (request) => [200, { prefix: { id: "p1", prefix: request.body!.prefix, to: request.body!.to, write_protection: "lock", databases: 0 } }]);
  daemon.route("POST /v1/replicas/stop", () => [200, {}]);
  const client = await Litebackup.connect({ socketPath: daemon.socketPath });
  const prefix = await client.replicatePrefix("tenants", { to: "replicas", writeProtection: "lock" });
  assert.equal(prefix.id, "p1");
  assert.equal(prefix.writeProtection, "lock");
  assert.deepEqual(daemon.received("/v1/replicas/start")[0]!.body, { prefix: "tenants", to: join(process.cwd(), "replicas"), write_protection: "lock" });
  await client.replicas.stopPrefix("tenants", "replicas");
  assert.deepEqual(daemon.received("/v1/replicas/stop")[0]!.body, { prefix: "tenants", to: join(process.cwd(), "replicas") });
  await client.replicatePrefix("", { to: "all" });
  assert.equal(daemon.received("/v1/replicas/start")[1]!.body!.prefix, "");
  await assert.rejects(client.replicatePrefix("tenants", { to: "" }), (error: unknown) => error instanceof LitebackupError && error.code === "invalid_argument");
  await assert.rejects(client.replicas.stopPrefix("tenants", ""), LitebackupError);
  daemon.route("POST /v1/replicas/start", () => [200, {}]);
  await assert.rejects(client.replicatePrefix("tenants", { to: "replicas" }), (error: unknown) => error instanceof LitebackupError && error.code === "invalid_response");
  daemon.route("POST /v1/replicas/start", () => [403, { code: "forbidden", message: "namespace not allowed" }]);
  await assert.rejects(client.replicatePrefix("tenants", { to: "replicas" }), (error: unknown) => error instanceof LitebackupError && error.code === "forbidden");
});

test("foreground backup rejects cancellation before launch", async () => {
  await assert.rejects(backup("main.db", { signal: AbortSignal.abort() }), (e: unknown) => e instanceof LitebackupError && e.code === "cancelled");
});
