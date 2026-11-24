import { LitebackupError } from "./types.js";
import { lstatSync, mkdirSync, readFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import { join } from "node:path";

/** The session used when neither the caller nor LITEBACKUP_SESSION names one. */
export const defaultSession = "default";

/** Records a `litebackup install service` install, as litebackup reads it. */
export const systemConfigurationPath = "/etc/litebackup/client.json";

/** Matches litebackup's session names, which start with a letter or digit. */
const sessionPattern = /^[a-z0-9][a-z0-9._-]*$/;

/** The service install: who runs it and where its sessions live. */
interface SystemConfiguration {
  uid: number;
  runtime_directory: string;
}

/** Reads the service install, or undefined when there is none. */
function systemConfiguration(path: string): SystemConfiguration | undefined {
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as Partial<SystemConfiguration>;
    if (typeof parsed.uid === "number" && typeof parsed.runtime_directory === "string" && parsed.runtime_directory) {
      return { uid: parsed.uid, runtime_directory: parsed.runtime_directory };
    }
  } catch {
    // No service install, or one this SDK cannot read: use the per-user socket.
  }
  return undefined;
}

/** A user service install: where its sessions' sockets live. */
interface UserConfiguration {
  runtime_directory: string;
}

/**
 * Reads ~/.config/litebackup/service.json, found from HOME alone, or
 * undefined when this user installed no service.
 */
function userConfiguration(home: string): UserConfiguration | undefined {
  try {
    const parsed = JSON.parse(readFileSync(join(home, ".config", "litebackup", "service.json"), "utf8")) as Partial<UserConfiguration>;
    if (typeof parsed.runtime_directory === "string" && parsed.runtime_directory) {
      return { runtime_directory: parsed.runtime_directory };
    }
  } catch {
    // No user service, or one this SDK cannot read: use the environment.
  }
  return undefined;
}

/**
 * Resolves the session name and its socket exactly as litebackup does. Where
 * `litebackup install service` ran as root, root and the service's user use
 * the service's socket under its runtime directory; where a user installed a
 * service of their own, that user uses the directory it recorded, whatever
 * this process's environment. Everyone else uses
 * $XDG_RUNTIME_DIR/litebackup/<session>/client.sock, or a per-user directory
 * under the temporary directory, which must be this user's alone.
 */
export function sessionSocket(
  session: string | undefined,
  environment: NodeJS.ProcessEnv,
  configurationPath: string = systemConfigurationPath,
): { session: string; socketPath: string } {
  const name = session || environment.LITEBACKUP_SESSION || defaultSession;
  if (!sessionPattern.test(name)) {
    throw new LitebackupError("invalid_argument", `session names use [a-z0-9._-] and start with a letter or digit, got ${JSON.stringify(name)}`, 0);
  }
  const uid = userInfo().uid;
  const system = systemConfiguration(configurationPath);
  if (system && (uid === 0 || uid === system.uid)) {
    return { session: name, socketPath: join(system.runtime_directory, name, "client.sock") };
  }
  const user = userConfiguration(environment.HOME || homedir());
  if (user) {
    return { session: name, socketPath: join(user.runtime_directory, name, "client.sock") };
  }
  let runtimeRoot = environment.XDG_RUNTIME_DIR;
  if (!runtimeRoot) {
    runtimeRoot = join(environment.TMPDIR || tmpdir(), `litebackup-${uid}`);
    privateRuntimeDirectory(runtimeRoot, uid);
  }
  return { session: name, socketPath: join(runtimeRoot, "litebackup", name, "client.sock") };
}

/**
 * Makes sure the per-user runtime directory used without XDG_RUNTIME_DIR is
 * this user's own, as litebackup does: the temporary directory is shared, so
 * another local user could create it first and answer as the daemon. It
 * creates the directory with mode 0700 when it is missing, and refuses one
 * that is a symlink, is not a directory, belongs to another user, or is open
 * to anyone else.
 */
function privateRuntimeDirectory(directory: string, uid: number): void {
  try {
    mkdirSync(directory, { mode: 0o700 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
      throw new LitebackupError("invalid_argument", `creating ${directory} failed: ${String(error)}`, 0, { cause: error });
    }
  }
  const status = lstatSync(directory);
  if (!status.isDirectory() || status.uid !== uid || (status.mode & 0o077) !== 0) {
    throw new LitebackupError(
      "invalid_argument",
      `${directory} is not a directory of this user's alone; remove it or set XDG_RUNTIME_DIR to this user's own directory`,
      0,
    );
  }
}
