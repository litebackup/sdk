import { execFile } from "node:child_process";
import { closeSync, existsSync, openSync, readSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { createConnection } from "node:net";
import { homedir } from "node:os";
import { delimiter, dirname, join } from "node:path";

import { ErrorCode, LitebackupError } from "./types.js";

/** Overrides the litebackup binary the SDK starts. */
export const binaryPathEnvironment = "LITEBACKUP_PATH";

/**
 * Holds the credential a daemon uses when it starts: an API key, or a join
 * token the daemon exchanges once for a key of the machine, then @ and the
 * server's address.
 */
export const apiKeyEnvironment = "LITEBACKUP_API_KEY";

/** Prefixes that tell the credentials apart; join and invite tokens start with longer prefixes of an API key's. */
const apiKeyPrefix = "litebackup_";
const joinTokenPrefix = "litebackup_join_";
const invitePrefix = "litebackup_invite_";

/** What a LITEBACKUP_API_KEY value holds. */
export type CredentialType = "api_key" | "join_token";

/**
 * Tells an API key from a join token by prefix, as the daemon does, and
 * returns undefined for an empty credential, which leaves the daemon its
 * saved key. Throws invalid_argument, without repeating the credential, for
 * an invite token, a credential with neither prefix, or one without @ and
 * the server's address: a host, or an http:// or https:// URL of one.
 */
export function credentialType(keyAndServer: string | undefined): CredentialType | undefined {
  const trimmed = (keyAndServer ?? "").trim();
  if (!trimmed) return undefined;
  const separator = trimmed.lastIndexOf("@");
  const secret = separator > 0 ? trimmed.slice(0, separator) : "";
  if (!secret || !serverAddressPattern.test(trimmed.slice(separator + 1))) {
    throw new LitebackupError(ErrorCode.invalidArgument, "expected <API_KEY>@<HOST>: an API key or a join token from Machines → Add machine, then @ and the server's address: a host, or an http:// or https:// URL of one", 0);
  }
  if (secret.startsWith(invitePrefix)) {
    throw new LitebackupError(ErrorCode.invalidArgument, "this is an invite token, which signs a person in to the console; use an API key or a join token from Machines → Add machine", 0);
  }
  if (secret.startsWith(joinTokenPrefix)) return "join_token";
  if (secret.startsWith(apiKeyPrefix)) return "api_key";
  throw new LitebackupError(ErrorCode.invalidArgument, `expected an API key or a join token, which start with ${apiKeyPrefix}; get one from Machines → Add machine`, 0);
}

/**
 * The server address a credential may name, as the daemon checks it: an
 * optional http:// or https:// in any case, a host name or a bracketed IPv6
 * address, an optional port, and an optional path without a query or
 * fragment, of ASCII URL characters and percent escapes.
 */
const serverAddressPattern = /^(?:[Hh][Tt][Tt][Pp][Ss]?:\/\/)?(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:\/(?:[A-Za-z0-9._~!$&'()*+,;=:\/-]|%[0-9A-Fa-f]{2})*)?$/;

/** npm platform packages that bundle the litebackup binary. */
const platformPackages: Record<string, string> = {
  "darwin-arm64": "@litebackup/bin-darwin-arm64",
  "darwin-x64": "@litebackup/bin-darwin-x64",
  "linux-arm64": "@litebackup/bin-linux-arm64",
  "linux-x64": "@litebackup/bin-linux-x64",
};

/**
 * The one line that installs the litebackup command on Linux and macOS for
 * whoever runs it: into ~/.local/bin, or /usr/local/bin as root.
 */
export function installCommand(): string {
  return "curl -fsSL https://litebackup.com/cli.sh | bash";
}

/** Finds litebackup on PATH, or returns undefined. */
function onPath(environment: NodeJS.ProcessEnv): string | undefined {
  for (const directory of (environment.PATH ?? "").split(delimiter)) {
    if (directory && existsSync(join(directory, "litebackup"))) {
      return join(directory, "litebackup");
    }
  }
  return undefined;
}

/**
 * Finds the litebackup binary: the explicit path, then LITEBACKUP_PATH, then
 * this platform's optional npm package, then litebackup on PATH, then
 * ~/.local/bin. Throws binary_not_found with the install command when there is none, or
 * when the explicit path or LITEBACKUP_PATH names no file.
 */
export function litebackupBinary(environment: NodeJS.ProcessEnv = process.env, binaryPath?: string): string {
  const override = binaryPath?.trim() || environment[binaryPathEnvironment]?.trim();
  if (override) {
    // An explicit path that names no file never falls back to another binary;
    // a file that cannot run fails later, as launch_failed.
    if (missingPath(override)) {
      throw new LitebackupError(ErrorCode.binaryNotFound, `the litebackup binary ${override} does not exist; set binaryPath or ${binaryPathEnvironment} to it, or install it with: ${installCommand()}`, 0);
    }
    return override;
  }
  const packageName = platformPackages[`${process.platform}-${process.arch}`];
  if (packageName) {
    try {
      const require = createRequire(import.meta.url);
      const binary = join(dirname(require.resolve(`${packageName}/package.json`)), "bin", "litebackup");
      if (existsSync(binary)) {
        return binary;
      }
    } catch {
      // The optional package is not installed for this platform; fall back to PATH.
    }
  }
  const found = onPath(environment);
  if (found) {
    return found;
  }
  // installCommand puts it in ~/.local/bin for a user other than root, which
  // an app's PATH may not include.
  const local = join(environment.HOME || homedir(), ".local", "bin", "litebackup");
  if (existsSync(local)) {
    return local;
  }
  throw new LitebackupError("binary_not_found", `the litebackup command is not installed; set ${binaryPathEnvironment} or install it with: ${installCommand()}`, 0);
}

/** Reports whether something accepts connections on the socket. */
export function socketListening(socketPath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const connection = createConnection({ path: socketPath });
    connection.once("connect", () => {
      connection.end();
      resolve(true);
    });
    connection.once("error", () => resolve(false));
  });
}

/**
 * The code and message of the last error the launch command reported as JSON,
 * or launch_failed and its whole error output when it reported none. The code
 * is drain_required when the launch refused another credential for a client
 * that holds write locks, and launch_failed otherwise.
 */
export function launchDenial(stderr: string): { code: string; message: string } {
  let denial = "";
  let denialCode: string = ErrorCode.launchFailed;
  for (const line of stderr.split("\n")) {
    let launchDiagnostic: unknown;
    try {
      launchDiagnostic = JSON.parse(line);
    } catch {
      continue;
    }
    if (typeof launchDiagnostic !== "object" || launchDiagnostic === null) continue;
    const { event, code, message } = launchDiagnostic as Record<string, unknown>;
    // A warning carries an event; an error does not.
    if (event === undefined && typeof code === "string" && typeof message === "string" && message !== "") {
      denial = message;
      denialCode = code === ErrorCode.drainRequired ? ErrorCode.drainRequired : ErrorCode.launchFailed;
    }
  }
  return denial ? { code: denialCode, message: denial } : { code: ErrorCode.launchFailed, message: stderr.trim() };
}

/**
 * Starts the session daemon when nothing listens. It runs litebackup's own
 * launch command, which takes the session's launch lock and starts
 * `litebackup daemon --session`, exactly like the CLI does, so concurrent
 * processes start one daemon. The daemon uses the credential in the
 * environment's LITEBACKUP_API_KEY. Where `litebackup install service` ran,
 * systemd or launchd owns the daemon, and the launch command explains how to
 * reach or start the service instead.
 */
export async function launchDaemon(args: { session: string; socketPath: string; environment: NodeJS.ProcessEnv; binaryPath?: string; signal?: AbortSignal }): Promise<void> {
  if (await socketListening(args.socketPath)) {
    return;
  }
  const binary = litebackupBinary(args.environment, args.binaryPath);
  const environment: NodeJS.ProcessEnv = { ...args.environment, [binaryPathEnvironment]: binary };
  await new Promise<void>((resolve, reject) => {
    // The signal ends the launch command when the client closes; callers that only stop waiting leave it to finish for the others.
    execFile(binary, ["launch", "--session", args.session, "--stdout-format", "json"], { env: environment, timeout: 30_000, signal: args.signal }, (error, _stdout, stderr) => {
      if (error) {
        // A binary that is there but does not start names why, not a missing file.
        const startFailure = error as NodeJS.ErrnoException;
        if (missingFileCode(startFailure.code)) {
          reject(new LitebackupError(ErrorCode.launchFailed, startFailureMessage(binary, startFailure), 0, { cause: error }));
          return;
        }
        const denial = launchDenial(stderr);
        reject(new LitebackupError(denial.code, `starting Litebackup failed: ${denial.message || error.message}`, 0, { cause: error }));
        return;
      }
      resolve();
    });
  });
}

/**
 * Reports whether nothing is at path: it, or a directory on its way, does not
 * exist. A path behind a directory this user may not enter is not missing.
 */
export function missingPath(path: string): boolean {
  try {
    statSync(path);
    return false;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return missingFileCode(code);
  }
}

/**
 * Says why starting binary failed. A file that is there but fails to start
 * as missing (ENOENT or ENOTDIR) cannot run: the interpreter its #! line
 * names, or its dynamic loader, is not on this machine. The message names
 * that line rather than repeating the file's path as if it were missing.
 */
export function startFailureMessage(binary: string, err: NodeJS.ErrnoException): string {
  if (!missingFileCode(err.code) || missingPath(binary)) {
    return `starting ${binary} failed: ${err.message}`;
  }
  const interpreterLine = scriptInterpreterLine(binary);
  const unusableBinary = `starting ${binary} failed: it cannot run because`;
  if (interpreterLine === "") {
    return `${unusableBinary} its interpreter or dynamic loader is not on this machine`;
  }
  return `${unusableBinary} the interpreter its first line names is not on this machine: ${interpreterLine}`;
}

/** The #! line that starts the file at path, or "" when it starts with none or cannot be read. */
export function scriptInterpreterLine(path: string): string {
  let start: Buffer;
  try {
    const descriptor = openSync(path, "r");
    try {
      start = Buffer.alloc(256);
      const count = readSync(descriptor, start, 0, start.length, 0);
      start = start.subarray(0, count);
    } finally {
      closeSync(descriptor);
    }
  } catch {
    return "";
  }
  const firstLine = start.toString("utf8").split("\n", 1)[0] ?? "";
  return firstLine.startsWith("#!") ? firstLine.trim() : "";
}

/** Reports whether an error code says a file, or a directory on its way, is not there: ENOENT or ENOTDIR. */
export function missingFileCode(code: string | undefined): boolean {
  return code === "ENOENT" || code === "ENOTDIR";
}
