// Runs the language-neutral fixtures in testdata/contract, which the Go and
// Python SDKs run too.
import assert from "node:assert/strict";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { test, type TestContext } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { Litebackup, LitebackupError, minimumDaemonVersion, sdkVersion, type ScopeAdapter } from "../src/index.js";

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

/** Lists a fixture kind's names. */
function fixtureNames(kind: string): string[] {
  return readdirSync(join(contractRoot, kind))
    .filter((name) => name.endsWith(".json"))
    .map((name) => name.slice(0, -5))
    .sort();
}

/** Reads a fixture with {cwd} replaced. */
function readFixture<T>(kind: string, name: string, cwd = "/"): T {
  const text = readFileSync(join(contractRoot, kind, `${name}.json`), "utf8");
  return JSON.parse(text.replaceAll("{cwd}", JSON.stringify(cwd).slice(1, -1))) as T;
}

/** A daemon on a unix socket that answers the connect probe itself and hands every other request to reply. */
async function contractDaemon(t: TestContext, reply: (request: Received) => Reply, clientVersion: string | null = "1.0.0") {
  const directory = await mkdtemp(join(tmpdir(), "lbc-"));
  const socketPath = join(directory, "client.sock");
  const received: Received[] = [];
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const url = new URL(req.url ?? "/", "http://litebackup");
      const request: Received = { method: req.method ?? "", path: url.pathname, query: Object.fromEntries(url.searchParams), body: Buffer.concat(chunks).toString("utf8") };
      assert.equal(req.headers["x-litebackup-sdk-version"], `typescript/${sdkVersion}`);
      if (clientVersion !== null) res.setHeader("x-litebackup-client-version", clientVersion);
      if (request.method === "GET" && request.path === "/v1/status/show" && JSON.stringify(request.query) === JSON.stringify({ limit: "1" })) {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ session: "contract", server: "http://example", client_version: "1.0.0", session_id: "s", watch: "inotify", databases: [] }));
        return;
      }
      received.push(request);
      const [status, contentType, body] = reply(request);
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
      return client.session.shutdown({ wait: args.wait as "cloud" | undefined, timeout: args.timeout as string | undefined });
    case "databases.page":
      return client.databases.page({ after: args.after as string | undefined, limit: number("limit") });
    case "databases.get":
      return client.databases.get(text("name"));
    case "databases.sync":
      return client.databases.sync(text("name"));
    case "databases.offload":
      return client.databases.offload(text("name"));
    case "databases.writer":
      return client.databases.writer(text("name"));
    case "databases.restore":
      return client.databases.restore(text("name"), { to: pathArg(args.to, cwd)!, version: number("version"), time: args.time as string | undefined });
    case "databases.acquire":
      return client.databases.acquire(text("name"), {
        to: pathArg(args.to, cwd),
        maintenance: args.maintenance as boolean | undefined,
        offload: args.offload as "released" | undefined,
        machineLabel: args.machine_label as string | undefined,
        onLost: args.on_lost as "block" | undefined,
        timeout: args.timeout as string | undefined,
      });
    case "databases.release":
      return client.databases.release(text("name"), { wait: args.wait as "cloud" | undefined, offload: args.offload as boolean | undefined, timeout: args.timeout as string | undefined });
    case "backups.start":
      return client.backups.start(pathArg(args.source, cwd)!, { name: args.name as string | undefined, prefix: args.prefix as string | undefined, maximumLocalDatabases: number("maximum_local_databases") });
    case "backups.stop":
      return client.backups.stop(text("name"));
    case "rules.list":
      return client.rules.list();
    case "rules.stop":
      return client.rules.stop(text("id"));
    case "replicas.start":
      return client.replicas.start(text("name"), { to: pathArg(args.to, cwd), writeProtection: args.write_protection as "detect" | undefined, timeout: args.timeout as string | undefined });
    case "replicas.start_prefix":
      return client.replicas.startPrefix(text("prefix"), { to: pathArg(args.to, cwd)! });
    case "replicas.list":
      return client.replicas.list();
    case "replicas.stop":
      return client.replicas.stop(text("name"));
    case "replicas.stop_prefix":
      return client.replicas.stopPrefix(text("prefix"), pathArg(args.to, cwd)!);
    case "conflicts.list":
      return client.conflicts.list();
    case "conflicts.delete":
      return client.conflicts.delete(text("id"));
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
      const outcome = invoke(daemon.client, fixture.operation, fixture.args, cwd);
      if (fixture.error) {
        await assert.rejects(outcome, (error: unknown) => error instanceof LitebackupError && error.code === fixture.error!.code);
        assert.equal(daemon.received.length, 0);
        return;
      }
      await outcome;
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
  take?: number;
  exchanges: Exchange[];
  skip?: Record<string, string>;
  expect: { items: Json[]; error?: { code: string } };
}

for (const kind of ["pagination", "events"]) {
  for (const name of fixtureNames(kind)) {
    const fixture = readFixture<PageFixture>(kind, name);
    test(`contract ${kind} ${name}`, { skip: fixture.skip?.typescript }, async (t) => {
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
      const take = fixture.take ?? Infinity;
      const iterable: AsyncIterable<Record<string, Json>> | undefined =
        fixture.operation === "databases.iterate"
          ? (daemon.client.databases.iterate() as AsyncIterable<never>)
          : fixture.operation === "databases.versions"
            ? (daemon.client.databases.iterateVersions(fixture.args.name as string, { after: fixture.args.after as number | undefined, limit: fixture.args.limit as number | undefined }) as AsyncIterable<never>)
            : fixture.operation === "events.watch"
              ? (daemon.client.events.watch({ after: fixture.args.after as number | undefined, wait: fixture.args.wait as string | undefined }) as AsyncIterable<never>)
              : undefined;
      assert.ok(iterable, `no TypeScript mapping for ${fixture.operation}`);
      try {
        for await (const item of iterable) {
          items.push(item[fixture.item_key]!);
          if (items.length >= take) break;
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
  scope: { name: string; to?: string; durability?: string; offload?: boolean; cleanup_timeout?: string; create?: boolean };
  fail: string[];
  cancel?: string;
  acquire_response: Json;
  skip?: Record<string, string>;
  expect: { steps: string[]; acquire_body?: Json; open_path?: string; release_body: Json; raised: RaisedError[]; created_file?: string; missing_file?: string };
}

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
        return [200, "application/json", JSON.stringify({ database: { name: fixture.scope.name, path: "/x", role: "origin", state: "synced" }, checkpointed: false, vacuumed: false, offloaded: false })];
      });
      await daemon.client.connect();
      const openError = new Error("open failed");
      const closeError = new Error("close failed");
      let openPath: string | undefined;
      const adapter: ScopeAdapter<string> = {
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
      let raised: unknown[] = [];
      try {
        await daemon.client.databases.managed(
          fixture.scope.name,
          {
            to: fixture.scope.to,
            durability: fixture.scope.durability as "cloud",
            offload: fixture.scope.offload,
            cleanupTimeout: fixture.scope.cleanup_timeout,
            create: fixture.scope.create,
            adapter,
            signal: controller.signal,
          },
          (_connection, scope) => {
            steps.push("work");
            if (fixture.cancel === "work") {
              controller.abort(new Error("work cancelled"));
              workError = scope.signal.reason;
              throw workError;
            }
            if (failing.has("work")) {
              workError = new Error("work failed");
              throw workError;
            }
          },
        );
      } catch (error) {
        raised = error instanceof AggregateError ? error.errors : [error];
      }
      assert.deepEqual(steps, fixture.expect.steps);
      if (fixture.expect.open_path) assert.equal(openPath, fixture.expect.open_path);
      if (fixture.expect.created_file) assert.ok(existsSync(fixture.expect.created_file), "the scope did not create the file");
      if (fixture.expect.missing_file) assert.ok(!existsSync(fixture.expect.missing_file), "the scope created the file");
      for (const request of daemon.received) {
        const expected = request.path.endsWith("/acquire") ? fixture.expect.acquire_body : fixture.expect.release_body;
        assert.deepEqual(decoded(request.body), expected, request.path);
      }
      assert.equal(raised.length, fixture.expect.raised.length, `raised ${raised.map(String).join("; ")}`);
      fixture.expect.raised.forEach((expected, index) => {
        const actual = raised[index];
        if (expected.source === "work") return assert.equal(actual, workError);
        if (expected.source === "open") return assert.equal(actual, openError);
        assert.ok(actual instanceof LitebackupError && actual.code === expected.code, `error ${index} is ${String(actual)}, want ${expected.code}`);
        if (expected.details) assert.deepEqual(actual.details, expected.details);
        if (expected.cause === "close") assert.equal(actual.cause, closeError);
        if (expected.cause === "release") assert.ok(actual.cause instanceof LitebackupError && actual.cause.code === "unavailable");
      });
    });
  });
}
