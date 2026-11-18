import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { createConnection } from "node:net";
import { homedir } from "node:os";
import { delimiter, dirname, join } from "node:path";

/** Overrides the litebackup binary the SDK starts. */
export const binaryPathEnvironment = "LITEBACKUP_PATH";

/** Holds the join token, TOKEN@HOST, a daemon joins with on its first start. */
export const tokenEnvironment = "LITEBACKUP_TOKEN";

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
 * Finds the litebackup binary: LITEBACKUP_PATH, then this platform's
 * optional npm package, then litebackup on PATH, then ~/.local/bin.
 */
export function litebackupBinary(environment: NodeJS.ProcessEnv = process.env): string {
  const override = environment[binaryPathEnvironment]?.trim();
  if (override) {
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
  throw new Error(`the litebackup command is not installed; set ${binaryPathEnvironment} or install it with: ${installCommand()}`);
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
 * Starts the session daemon when nothing listens. It runs litebackup's own
 * launch command, which takes the session's launch lock and starts
 * `litebackup daemon --session`, exactly like the CLI does, so concurrent
 * processes start one daemon. The daemon joins with the token on its first
 * start. Where `litebackup install service` ran, systemd or launchd owns the daemon,
 * and the launch command explains how to reach or start the service instead.
 */
export async function launchDaemon(args: { session: string; socketPath: string; environment: NodeJS.ProcessEnv; token?: string }): Promise<void> {
  if (await socketListening(args.socketPath)) {
    return;
  }
  const binary = litebackupBinary(args.environment);
  const environment: NodeJS.ProcessEnv = { ...args.environment, [binaryPathEnvironment]: binary };
  if (args.token) {
    environment[tokenEnvironment] = args.token;
  }
  await new Promise<void>((resolve, reject) => {
    execFile(binary, ["launch", "--session", args.session], { env: environment, timeout: 30_000 }, (error, _stdout, stderr) => {
      if (error) {
        reject(new Error(`starting Litebackup failed: ${stderr.trim() || error.message}`));
        return;
      }
      resolve();
    });
  });
}
