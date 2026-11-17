import { request } from "node:http";

/** A response read whole from the client socket. */
export interface SocketResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
}

/** Options for one request over the client's unix socket. */
export interface SocketRequest {
  socketPath: string;
  method: "GET" | "POST";
  path: string;
  headers: Record<string, string>;
  body?: string;
  signal?: AbortSignal;
}

/**
 * Sends one HTTP request over a unix socket and reads the whole response.
 * The client API answers small JSON documents, so buffering is fine.
 */
export function socketFetch(options: SocketRequest): Promise<SocketResponse> {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      {
        socketPath: options.socketPath,
        method: options.method,
        path: options.path,
        headers: {
          ...options.headers,
          ...(options.body === undefined ? {} : { "content-type": "application/json", "content-length": Buffer.byteLength(options.body) }),
        },
        signal: options.signal,
      },
      (incoming) => {
        const chunks: Buffer[] = [];
        incoming.on("data", (chunk: Buffer) => chunks.push(chunk));
        incoming.on("error", reject);
        incoming.on("end", () => {
          const headers: Record<string, string> = {};
          for (const [name, value] of Object.entries(incoming.headers)) {
            if (typeof value === "string") {
              headers[name] = value;
            } else if (Array.isArray(value)) {
              headers[name] = value.join(", ");
            }
          }
          resolve({
            status: incoming.statusCode ?? 0,
            headers,
            body: Buffer.concat(chunks).toString("utf8"),
          });
        });
      },
    );
    outgoing.on("error", reject);
    if (options.body !== undefined) {
      outgoing.write(options.body);
    }
    outgoing.end();
  });
}
