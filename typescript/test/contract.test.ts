// Runs the language-neutral fixtures in testdata/contract, which the Go and
// Python SDKs run too.
import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { test, type TestContext } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { ErrorCode, LeaseCleanupError, Litebackup, LitebackupError, minimumDaemonVersion, sdkVersion, type Adapter, type Pool } from "../src/index.js";
import { startFailure } from "../src/backup.js";
import { credentialType, litebackupBinary, type CredentialType } from "../src/launch.js";

const contractRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "testdata", "contract");

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Args = Record<string, Json>;

/** A request the contract daemon received. */
interface Received {
  method: string;
  path: string;
  query: Record<string, string>;
  body: string;
}

/** An answer: status, content type, and body text. */
type Reply = [number, string, string];

/** Lists the fixture names in one fixture directory. */
function fixtureNames(fixtureDirectory: string): string[] {
  return readdirSync(join(contractRoot, fixtureDirectory))
    .filter((name) => name.endsWith(".json"))
    .map((name) => name.slice(0, -5))
    .sort();
}

/** Reads a fixture with {cwd} and "{umask}", this process's umask, replaced. */
function readFixture<T>(fixtureDirectory: string, name: string, cwd = "/"): T {
  const text = readFileSync(join(contractRoot, fixtureDirectory, `${name}.json`), "utf8");
  const substituted = text.replaceAll("{cwd}", JSON.stringify(cwd).slice(1, -1)).replaceAll('"{umask}"', String(process.umask()));
  return JSON.parse(substituted) as T;
}

/** A daemon on a unix socket that answers the connect probe itself and hands every other request to reply. */
async function contractDaemon(t: TestContext, reply: (request: Received) => Reply | Promise<Reply>, clientVersion: string | null = "1.1.0") {
  const directory = await mkdtemp(join(tmpdir(), "lbc-"));
  const socketPath = join(directory, "client.sock");
  const received: Received[] = [];
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", async () => {
      const url = new URL(req.url ?? "/", "http://litebackup");
      const request: Received = { method: req.method ?? "", path: url.pathname, query: Object.fromEntries(url.searchParams), body: Buffer.concat(chunks).toString("utf8") };
      assert.equal(req.headers["litebackup-sdk-version"], `typescript/${sdkVersion}`);
      if (clientVersion !== null) res.setHeader("litebackup-client-version", clientVersion);
      if (request.method === "GET" && request.path === "/v1/status/show" && JSON.stringify(request.query) === JSON.stringify({ limit: "1" })) {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ session: "contract", server: "http://example", client_version: "1.1.0", session_id: "s", watch: "inotify", databases: [] }));
        return;
      }
      received.push(request);
      const [status, contentType, body] = await reply(request);
      res.statusCode = status;
      res.setHeader("content-type", contentType);
      res.end(body);
    });
  });
  await new Promise<void>((done) => server.listen(socketPath, done));
  const client = new Litebackup({ socketPath });
  t.after(async () => {
    client.close();
    server.closeAllConnections();
    await new Promise<void>((done) => server.close(() => done()));
    await rm(directory, { recursive: true, force: true });
  });
  return { client, received, socketPath };
}

/** Runs body in a fresh working directory, its symlinks resolved. */
async function inDirectory(t: TestContext, body: (cwd: string) => Promise<void>): Promise<void> {
  const cwd = await realpath(await mkdtemp(join(tmpdir(), "lbcwd-")));
  const previous = process.cwd();
  process.chdir(cwd);
  try {
    await body(cwd);
  } finally {
    process.chdir(previous);
    await rm(cwd, { recursive: true, force: true });
  }
  void t;
}

/** A fixture argument as TypeScript takes it: {"$path"} becomes a file: URL. */
function pathArg(value: Json | undefined, cwd: string): string | URL | undefined {
  if (value && typeof value === "object" && !Array.isArray(value) && typeof value.$path === "string") {
    return pathToFileURL(resolve(cwd, value.$path));
  }
  return value as string | undefined;
}

/** Runs a fixture operation with the TypeScript API. */
async function invoke(client: Litebackup, operation: string, args: Args, cwd: string): Promise<unknown> {
  const text = (key: string) => args[key] as string;
  const number = (key: string) => args[key] as number | undefined;
  switch (operation) {
    case "session.status":
      return client.session.status({ after: args.after as string | undefined, limit: number("limit") });
    case "session.shutdown":
      return client.session.shutdown({ durability: args.durability as "cloud" | undefined, timeout: args.timeout as string | undefined });
    case "databases.page":
      return client.databases.page({ after: args.after as string | undefined, limit: number("limit") });
    case "databases.get":
      return client.databases.get(text("name"));
    case "databases.sync":
      return client.databases.sync(text("name"));
    case "databases.offload":
      return client.databases.offload(text("name"), { durability: args.durability as "server" | "cloud" | undefined });
    case "databases.writer":
      return client.databases.writer(text("name"));
    case "databases.restore":
      return client.databases.restore(text("name"), { path: pathArg(args.path, cwd)!, version: number("version"), time: args.time as string | undefined, umask: number("umask") });
    case "databases.revert":
      return client.databases.revert(text("name"), { version: number("version"), time: args.time as string | undefined });
    case "databases.acquire":
      return client.databases.acquire(text("name"), {
        path: pathArg(args.path, cwd),
        maintenance: args.maintenance as boolean | undefined,
        offloadOnRelease: args.offload_on_release as boolean | undefined,
        offloadDurability: args.offload_durability as "server" | "cloud" | undefined,
        machineLabel: args.machine_label as string | undefined,
        timeout: args.timeout as string | undefined,
        lockTimeout: args.lock_timeout as string | undefined,
        umask: number("umask"),
        discardLocal: args.discard_local as boolean | undefined,
      });
    case "databases.release":
      return client.databases.release(text("name"), { durability: args.durability as "cloud" | undefined, offload: args.offload as boolean | undefined, timeout: args.timeout as string | undefined });
    case "backups.start":
      return client.backups.start(pathArg(args.source, cwd)!, { name: args.name as string | undefined, prefix: args.prefix as string | undefined, maximumLocalDatabases: number("maximum_local_databases"), offloadDurability: args.offload_durability as "server" | "cloud" | undefined, offloadRollbackJournal: args.offload_rollback_journal as boolean | undefined });
    case "backups.stop":
      return client.backups.stop(text("name"));
    case "rules.list":
      return client.rules.list();
    case "rules.stop":
      return client.rules.stop(text("id"));
    case "replicas.start":
      return client.replicas.start(text("name"), { path: pathArg(args.path, cwd), writeProtection: args.write_protection as "detect" | undefined, timeout: args.timeout as string | undefined });
    case "replicas.start_prefix":
      return client.replicas.startPrefix(text("prefix"), { directory: pathArg(args.directory, cwd)! });
    case "replicas.list":
      return client.replicas.list();
    case "replicas.stop":
      return client.replicas.stop(text("name"));
    case "replicas.stop_prefix":
      return client.replicas.stopPrefix(text("prefix"), pathArg(args.directory, cwd)!);
    case "events.page":
      return client.events.page({ after: number("after"), wait: args.wait as string | undefined, limit: number("limit") });
    default:
      throw new Error(`no TypeScript mapping for ${operation}`);
  }
}

/** Decodes a JSON body, or undefined for none. */
function decoded(body: string): unknown {
  return body ? JSON.parse(body) : undefined;
}

interface RequestFixture {
  operation: string;
  args: Args;
  request?: { method: string; path: string; query: Record<string, string>; body?: Json };
  response?: Json;
  error?: { code: string };
}

for (const name of fixtureNames("requests")) {
  test(`contract request ${name}`, async (t) => {
    await inDirectory(t, async (cwd) => {
      const fixture = readFixture<RequestFixture>("requests", name, cwd);
      const daemon = await contractDaemon(t, () => [200, "application/json", JSON.stringify(fixture.response ?? {})]);
      await daemon.client.connect();
      const invocation = invoke(daemon.client, fixture.operation, fixture.args, cwd);
      if (fixture.error) {
        await assert.rejects(invocation, (error: unknown) => error instanceof LitebackupError && error.code === fixture.error!.code);
        assert.equal(daemon.received.length, 0);
        return;
      }
      await invocation;
      assert.equal(daemon.received.length, 1);
      const [sent] = daemon.received;
      assert.equal(sent!.method, fixture.request!.method);
      assert.equal(sent!.path, fixture.request!.path);
      assert.deepEqual(sent!.query, fixture.request!.query);
      assert.deepEqual(decoded(sent!.body), fixture.request!.body);
    });
  });
}

interface ErrorFixture {
  response: { status: number; content_type: string; body: string };
  expect: { code: string; message: string; status: number; details: Record<string, unknown> };
}

for (const name of fixtureNames("errors")) {
  test(`contract error ${name}`, async (t) => {
    const fixture = readFixture<ErrorFixture>("errors", name);
    const daemon = await contractDaemon(t, () => [fixture.response.status, fixture.response.content_type, fixture.response.body]);
    await assert.rejects(daemon.client.rules.list(), (error: unknown) => {
      assert.ok(error instanceof LitebackupError);
      assert.equal(error.code, fixture.expect.code);
      assert.equal(error.message, `${fixture.expect.code}: ${fixture.expect.message}`);
      assert.equal(error.status, fixture.expect.status);
      assert.deepEqual(error.details, fixture.expect.details);
      return true;
    });
  });
}

interface VersionFixture {
  client_version: string | null;
  accepted: boolean;
  expect?: { code: string };
}

for (const name of fixtureNames("version")) {
  test(`contract version ${name}`, async (t) => {
    const fixture = readFixture<VersionFixture>("version", name);
    const daemon = await contractDaemon(t, () => [500, "text/plain", ""], fixture.client_version);
    if (fixture.accepted) {
      await daemon.client.connect();
      return;
    }
    await assert.rejects(daemon.client.connect(), (error: unknown) => {
      assert.ok(error instanceof LitebackupError);
      assert.equal(error.code, fixture.expect!.code);
      assert.match(error.message, new RegExp(minimumDaemonVersion.replaceAll(".", "\\.")));
      assert.match(error.message, /litebackup drain/);
      return true;
    });
  });
}

interface Exchange {
  request: { method: string; path: string; query: Record<string, string> };
  response: { status: number; body: Json };
}

interface PageFixture {
  operation: string;
  args: Args;
  item_key: string;
  item_limit?: number;
  exchanges: Exchange[];
  skip?: Record<string, string>;
  expect: { items: Json[]; error?: { code: string } };
}

/**
 * The release gets what closing left of the cleanup timeout, so its timeout_millis
 * may fall up to a second short of the fixture's full timeout; such a body is
 * returned with the fixture's value.
 */
function withSpentCleanup(body: Json, expected: Json | undefined): Json {
  const sent = body as Record<string, Json> | null;
  const want = expected as Record<string, Json> | undefined;
  if (!sent || !want || typeof sent.timeout_millis !== "number" || typeof want.timeout_millis !== "number") return body;
  const spent = want.timeout_millis - sent.timeout_millis;
  return spent >= 0 && spent <= 1000 ? { ...sent, timeout_millis: want.timeout_millis } : body;
}

for (const fixtureDirectory of ["pagination", "events"]) {
  for (const name of fixtureNames(fixtureDirectory)) {
    const fixture = readFixture<PageFixture>(fixtureDirectory, name);
    test(`contract ${fixtureDirectory} ${name}`, { skip: fixture.skip?.typescript }, async (t) => {
      let next = 0;
      const daemon = await contractDaemon(t, (request) => {
        const exchange = fixture.exchanges[next++];
        if (!exchange) {
          return [500, "application/json", JSON.stringify({ code: "unexpected", message: "no more exchanges" })];
        }
        assert.deepEqual({ method: request.method, path: request.path, query: request.query }, exchange.request);
        return [exchange.response.status, "application/json", JSON.stringify(exchange.response.body)];
      });
      const items: Json[] = [];
      let ended: unknown;
      const itemLimit = fixture.item_limit ?? Infinity;
      const iterable: AsyncIterable<Record<string, Json>> | undefined =
        fixture.operation === "databases.all"
          ? (daemon.client.databases.all() as AsyncIterable<never>)
          : fixture.operation === "databases.versions"
            ? (daemon.client.databases.allVersions(fixture.args.name as string, { after: fixture.args.after as number | undefined, limit: fixture.args.limit as number | undefined }) as AsyncIterable<never>)
            : fixture.operation === "events.watch"
              ? (daemon.client.events.watch({ after: fixture.args.after as number | undefined, wait: fixture.args.wait as string | undefined }) as AsyncIterable<never>)
              : undefined;
      assert.ok(iterable, `no TypeScript mapping for ${fixture.operation}`);
      try {
        for await (const item of iterable) {
          items.push(item[fixture.item_key] ?? null);
          if (items.length >= itemLimit) break;
        }
      } catch (error) {
        ended = error;
      }
      assert.deepEqual(items, fixture.expect.items);
      if (fixture.expect.error) {
        assert.ok(ended instanceof LitebackupError && ended.code === fixture.expect.error.code, `ended with ${String(ended)}`);
      } else if (ended) {
        throw ended;
      }
      assert.equal(daemon.received.length, fixture.exchanges.length);
    });
  }
}

interface RaisedError {
  source?: string;
  code?: string;
  step?: string;
  cause?: string;
  details?: Record<string, unknown>;
}

interface LifecycleFixture {
  lease: { name: string; path?: string; durability?: string; offload?: boolean; cleanup_timeout?: string; require_existing?: boolean };
  fail: string[];
  cancel?: string;
  acquire_response: Json;
  skip?: Record<string, string>;
  expect: {
    steps: string[];
    acquire_body?: Json;
    open_path?: string;
    release_body: Json;
    raised: RaisedError[];
    close_raised: RaisedError[];
    created_file?: string;
    missing_file?: string;
  };
}

/** The errors a rejection carries: an AggregateError's, one error alone, or none. */
function raisedErrors(error: unknown): unknown[] {
  if (error === undefined) return [];
  return error instanceof AggregateError ? error.errors : [error];
}

// Each fixture creates a pool, runs one lease with the fixture's settings, and closes the pool.
for (const name of fixtureNames("lifecycle")) {
  const skip = readFixture<LifecycleFixture>("lifecycle", name).skip?.typescript;
  test(`contract lifecycle ${name}`, { skip }, async (t) => {
    await inDirectory(t, async (cwd) => {
      const fixture = readFixture<LifecycleFixture>("lifecycle", name, cwd);
      const failing = new Set(fixture.fail);
      const steps: string[] = [];
      const daemon = await contractDaemon(t, (request) => {
        const step = request.path.replace("/v1/databases/", "");
        steps.push(step);
        if (failing.has(step)) return [503, "application/json", JSON.stringify({ code: "unavailable", message: `${step} failed` })];
        if (step === "acquire") return [200, "application/json", JSON.stringify(fixture.acquire_response)];
        if (step === "sync") return [200, "application/json", JSON.stringify({ name: fixture.lease.name, path: "/x", role: "origin", state: "synced", acquired: true })];
        return [200, "application/json", JSON.stringify({ database: { name: fixture.lease.name, path: "/x", role: "origin", state: "synced" }, checkpointed: false, vacuumed: false, offloaded: false })];
      });
      await daemon.client.connect();
      const openError = new Error("open failed");
      const closeError = new Error("close failed");
      let openPath: string | undefined;
      const adapter: Adapter<string> = {
        open: (path) => {
          steps.push("open");
          openPath = path;
          if (failing.has("open")) throw openError;
          return "connection";
        },
        close: () => {
          steps.push("close");
          if (failing.has("close")) throw closeError;
        },
      };
      const controller = new AbortController();
      if (fixture.cancel === "before") controller.abort(new Error("cancelled before"));
      let workError: unknown;
      let leaseError: unknown;
      let closeRaisedError: unknown;
      let pool: Pool<string> | undefined;
      try {
        pool = daemon.client.databases.pool({ adapter, cleanupTimeout: fixture.lease.cleanup_timeout });
      } catch (error) {
        leaseError = error;
      }
      if (pool) {
        try {
          await pool.lease(
            fixture.lease.name,
            {
              path: fixture.lease.path,
              durability: fixture.lease.durability as "cloud",
              offload: fixture.lease.offload,
              requireExisting: fixture.lease.require_existing === true,
              signal: controller.signal,
            },
            (_connection, signal) => {
              steps.push("work");
              if (fixture.cancel === "work") {
                controller.abort(new Error("work cancelled"));
                workError = signal.reason;
                throw workError;
              }
              if (fixture.cancel === "work-returns") {
                controller.abort(new Error("work cancelled"));
                return;
              }
              if (failing.has("work")) {
                workError = new Error("work failed");
                throw workError;
              }
            },
          );
        } catch (error) {
          leaseError = error;
        }
        try {
          await pool.close();
        } catch (error) {
          closeRaisedError = error;
        }
      }
      assert.deepEqual(steps, fixture.expect.steps);
      if (fixture.expect.open_path) assert.equal(openPath, fixture.expect.open_path);
      if (fixture.expect.created_file) assert.ok(existsSync(fixture.expect.created_file), "the lease did not create the file");
      if (fixture.expect.missing_file) assert.ok(!existsSync(fixture.expect.missing_file), "the lease created the file");
      for (const request of daemon.received) {
        const expected = request.path.endsWith("/acquire") ? fixture.expect.acquire_body : request.path.endsWith("/release") ? fixture.expect.release_body : undefined;
        if (expected === undefined) continue;
        assert.deepEqual(withSpentCleanup(decoded(request.body) as Json, expected), expected, request.path);
      }
      const requireRaised = (call: string, error: unknown, expectedErrors: RaisedError[]) => {
        const raised = raisedErrors(error);
        assert.equal(raised.length, expectedErrors.length, `${call} raised ${raised.map(String).join("; ")}`);
        expectedErrors.forEach((expected, index) => {
          const actual = raised[index];
          if (expected.source === "work") return assert.equal(actual, workError, call);
          if (expected.source === "open") return assert.equal(actual, openError, call);
          if (expected.code === "cancelled" && fixture.cancel === "work-returns") assert.equal((actual as Error).cause, controller.signal.reason);
          assert.ok(actual instanceof LitebackupError && actual.code === expected.code, `${call} error ${index} is ${String(actual)}, want ${expected.code}`);
          if (expected.details) assert.deepEqual(actual.details, expected.details);
          if (expected.step) assert.ok(actual instanceof LeaseCleanupError && actual.step === expected.step, `${call} error ${index} is ${String(actual)}, want a ${expected.step} LeaseCleanupError`);
          if (expected.cause === "close") assert.equal(actual.cause, closeError);
          if (expected.cause === "release") assert.ok(actual.cause instanceof LitebackupError && actual.cause.code === "unavailable");
        });
      };
      requireRaised("the lease", leaseError, fixture.expect.raised);
      requireRaised("closing the pool", closeRaisedError, fixture.expect.close_raised);
    });
  });
}

test("contract credentials", async (t) => {
  for (const name of fixtureNames("credentials")) {
    await t.test(name, () => {
      const fixture = readFixture<{ api_key: string; type?: CredentialType | null; expect?: { code: string } }>("credentials", name);
      if (fixture.expect) {
        const secret = fixture.api_key.trim().split("@")[0]!;
        assert.throws(() => credentialType(fixture.api_key), (error: unknown) =>
          error instanceof LitebackupError && error.code === fixture.expect!.code && (!secret || !error.message.includes(secret)));
        return;
      }
      assert.equal(credentialType(fixture.api_key), fixture.type ?? undefined);
    });
  }
});

test("contract status times: synced_server.time and synced_cloud.archived", async (t) => {
  const fixture = readFixture<RequestFixture>("requests", "databases-get");
  const daemon = await contractDaemon(t, () => [200, "application/json", JSON.stringify(fixture.response)]);
  const status = await daemon.client.databases.get(String(fixture.args.name));
  assert.equal(status.syncedServer.time, "2026-11-15T09:31:00Z");
  assert.equal(status.syncedCloud.archived, "2026-11-15T09:30:00Z");
});

test("contract shutdown: a database the drain could not drain, with its code", async (t) => {
  const fixture = readFixture<RequestFixture>("requests", "shutdown-all");
  const daemon = await contractDaemon(t, () => [200, "application/json", JSON.stringify(fixture.response)]);
  const shutdown = await daemon.client.session.shutdown();
  assert.equal(shutdown.released, 2);
  assert.deepEqual(shutdown.failed, [{ name: "orders.sqlite", code: ErrorCode.lockUnavailable, message: "another machine writes the database (worker-3)" }]);
});

test("contract shutdown: a drain that released everything has no failures", async (t) => {
  const fixture = readFixture<RequestFixture>("requests", "shutdown-default");
  const daemon = await contractDaemon(t, () => [200, "application/json", JSON.stringify(fixture.response)]);
  const shutdown = await daemon.client.session.shutdown();
  assert.deepEqual(shutdown.failed, []);
});

test("contract status offload_durability: an acquired database's, and none for one not acquired", async (t) => {
  const acquired = readFixture<RequestFixture>("requests", "acquire-all").response as { database: Json };
  const acquiredDaemon = await contractDaemon(t, () => [200, "application/json", JSON.stringify(acquired.database)]);
  assert.equal((await acquiredDaemon.client.databases.get("agents/42.sqlite")).offloadDurability, "cloud");
  const backedUp = readFixture<RequestFixture>("requests", "databases-get");
  const backedUpDaemon = await contractDaemon(t, () => [200, "application/json", JSON.stringify(backedUp.response)]);
  assert.equal((await backedUpDaemon.client.databases.get(String(backedUp.args.name))).offloadDurability, undefined);
});

test("contract binary: an override that names no file is binary_not_found", async (t) => {
  const fixture = JSON.parse(readFileSync(join(contractRoot, "binary", "override-missing.json"), "utf8")) as {
    missing_file: string;
    overrides: string[];
    code: string;
    message_contains: string;
    denied_file: string;
    interpreter_script: string;
    interpreter_code: string;
  };
  const directory = await mkdtemp(join(tmpdir(), "litebackup-override-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const missing = join(directory, fixture.missing_file);
  for (const override of fixture.overrides) {
    const environment: NodeJS.ProcessEnv = { PATH: process.env.PATH, HOME: directory };
    let option: string | undefined;
    if (override === "option") {
      option = missing;
    } else if (override === "environment") {
      environment.LITEBACKUP_PATH = missing;
    } else {
      assert.fail(`unknown override ${override}`);
    }
    assert.throws(
      () => litebackupBinary(environment, option),
      (error: unknown) =>
        error instanceof LitebackupError && error.code === fixture.code && error.message.includes(fixture.message_contains) && error.message.includes(missing),
      override,
    );
  }
});

test("contract binary: an override that exists resolves, and one that cannot run is launch_failed", async (t) => {
  const fixture = JSON.parse(readFileSync(join(contractRoot, "binary", "override-missing.json"), "utf8")) as {
    denied_file: string;
    interpreter_script: string;
    interpreter_code: string;
    interpreter_message_contains: string;
  };
  const directory = await mkdtemp(join(tmpdir(), "litebackup-override-"));
  t.after(async () => {
    await chmod(dirname(join(directory, fixture.denied_file)), 0o755).catch(() => undefined);
    await rm(directory, { recursive: true, force: true });
  });
  if (process.getuid?.() !== 0) {
    const denied = join(directory, fixture.denied_file);
    await mkdir(dirname(denied), { recursive: true });
    await writeFile(denied, "#!/bin/sh\n", { mode: 0o755 });
    await chmod(dirname(denied), 0);
    assert.equal(litebackupBinary({ PATH: process.env.PATH }, denied), denied);
  }
  const script = join(directory, "litebackup");
  await writeFile(script, fixture.interpreter_script, { mode: 0o755 });
  const client = new Litebackup({ binaryPath: script, session: "unit", environment: { PATH: process.env.PATH, HOME: directory } });
  t.after(() => client.close());
  await assert.rejects(
    client.backups.once(join(directory, "a.db")),
    (error: unknown) => error instanceof LitebackupError && error.code === fixture.interpreter_code && error.message.includes(fixture.interpreter_message_contains),
  );
});

test("contract binary: a foreground backup of an override under a file is binary_not_found", async (t) => {
  const fixture = JSON.parse(readFileSync(join(contractRoot, "binary", "override-missing.json"), "utf8")) as {
    code: string;
    not_directory_file: string;
  };
  const directory = await mkdtemp(join(tmpdir(), "litebackup-override-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const underFile = join(directory, fixture.not_directory_file);
  await writeFile(dirname(underFile), "a file, not a directory");
  const client = new Litebackup({ binaryPath: underFile, session: "unit", environment: { PATH: process.env.PATH, HOME: directory } });
  t.after(() => client.close());
  await assert.rejects(
    client.backups.once(join(directory, "a.db")),
    (error: unknown) => error instanceof LitebackupError && error.code === fixture.code,
  );
});

test("contract binary: a start error is binary_not_found only when the file is gone", async (t) => {
  const fixture = JSON.parse(readFileSync(join(contractRoot, "binary", "override-missing.json"), "utf8")) as {
    code: string;
    interpreter_code: string;
    not_directory_file: string;
  };
  const directory = await mkdtemp(join(tmpdir(), "litebackup-override-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const underFile = join(directory, fixture.not_directory_file);
  await writeFile(dirname(underFile), "a file, not a directory");
  const existing = join(directory, "litebackup");
  await writeFile(existing, "#!/bin/sh\n", { mode: 0o755 });
  for (const code of ["ENOTDIR", "ENOENT"]) {
    const startError = Object.assign(new Error(code), { code });
    assert.equal(startFailure(underFile, startError).code, fixture.code, code);
    assert.equal(startFailure(existing, startError).code, fixture.interpreter_code, code);
  }
});
