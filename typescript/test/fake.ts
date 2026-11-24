import { mkdtemp, rm } from "node:fs/promises";
import { createServer, type IncomingMessage, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { TestContext } from "node:test";

/** One request the fake daemon received. */
export interface FakeRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  body: Record<string, unknown> | undefined;
  headers: IncomingMessage["headers"];
}

/** An answer: status and JSON body. */
export type Answer = [number, unknown] | Promise<[number, unknown]>;

/** A unix-socket HTTP server that answers like the litebackup daemon. */
export class FakeDaemon {
  readonly requests: FakeRequest[] = [];
  readonly routes = new Map<string, (request: FakeRequest) => Answer>();
  private constructor(
    readonly directory: string,
    readonly socketPath: string,
    private readonly server: Server,
  ) {}

  /** Starts a fake daemon at <directory>/litebackup/<session>/client.sock, where XDG_RUNTIME_DIR=<directory> finds it. */
  static async start(t: TestContext, session = "unit"): Promise<FakeDaemon> {
    const directory = await mkdtemp(join(tmpdir(), "lb-"));
    const { mkdir } = await import("node:fs/promises");
    await mkdir(join(directory, "litebackup", session), { recursive: true });
    const socketPath = join(directory, "litebackup", session, "client.sock");
    let daemon: FakeDaemon | undefined;
    const server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", async () => {
        const url = new URL(req.url ?? "/", "http://litebackup");
        const text = Buffer.concat(chunks).toString("utf8");
        const request: FakeRequest = { method: req.method ?? "", path: url.pathname, query: url.searchParams, body: text ? JSON.parse(text) : undefined, headers: req.headers };
        daemon!.requests.push(request);
        res.setHeader("litebackup-client-version", "1.1.0");
        res.setHeader("content-type", "application/json");
        const route = daemon!.routes.get(`${request.method} ${request.path}`);
        if (!route) {
          res.statusCode = 404;
          res.end(JSON.stringify({ code: "not_found", message: `no route ${request.path}` }));
          return;
        }
        const [status, body] = await route(request);
        res.statusCode = status;
        res.end(JSON.stringify(body));
      });
    });
    daemon = new FakeDaemon(directory, socketPath, server);
    daemon.route("GET /v1/status/show", () => [200, { session, server: "http://example", client_version: "1.1.0", session_id: "s", watch: "inotify", databases: [] }]);
    await daemon.serve();
    t.after(async () => {
      await daemon!.stop();
      await rm(directory, { recursive: true, force: true });
    });
    return daemon;
  }

  /** Listens on the daemon's socket and answers there until stop. */
  async serve(): Promise<void> {
    await new Promise<void>((resolve) => this.server.listen(this.socketPath, resolve));
  }

  /** Closes the socket and every connection, as a daemon that exits does; calls then fail with connection_failed until serve. */
  async stop(): Promise<void> {
    if (!this.server.listening) return;
    this.server.closeAllConnections();
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
    await rm(this.socketPath, { force: true });
  }

  /** Sets the answer for "METHOD /path". */
  route(key: string, answer: (request: FakeRequest) => Answer): void {
    this.routes.set(key, answer);
  }

  /** The requests to one path. */
  received(path: string): FakeRequest[] {
    return this.requests.filter((request) => request.path === path);
  }
}

/** A canned DatabaseStatus answer in the daemon's snake_case. */
export function databaseStatus(name: string, role: string, state: string, server: number, cloud: number): Record<string, unknown> {
  return {
    name,
    path: `/var/lib/app/${name}`,
    role,
    state,
    acquired: false,
    failover: false,
    local_changed: false,
    synced_server: { version: server },
    synced_cloud: { version: cloud },
    journal_mode: "wal",
    page_count: 2,
    sync_count: 1,
    hint_misses: 0,
    last_full_scan_reason: "first",
  };
}
