import { readFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";

/** The session used when neither the caller nor LITEBACKUP_SESSION names one. */
export const defaultSession = "default";

/** Records a `litebackup install service` install, as litebackup reads it. */
export const systemConfigurationPath = "/etc/litebackup/client.json";

/** Matches litebackup's session names. */
const sessionPattern = /^[a-z0-9._-]+$/;

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

/**
 * Resolves the session name and its socket exactly as litebackup does. Where
 * `litebackup install service` ran, root and the service's user use the
 * service's socket under its runtime directory. Everyone else uses
 * $XDG_RUNTIME_DIR/litebackup/<session>/client.sock, or a per-user directory
 * under the temporary directory.
 */
export function sessionSocket(
  session: string | undefined,
  environment: NodeJS.ProcessEnv,
  configurationPath: string = systemConfigurationPath,
): { session: string; socketPath: string } {
  const name = session || environment.LITEBACKUP_SESSION || defaultSession;
  if (!sessionPattern.test(name)) {
    throw new Error(`session names use [a-z0-9._-], got ${JSON.stringify(name)}`);
  }
  const uid = userInfo().uid;
  const system = systemConfiguration(configurationPath);
  if (system && (uid === 0 || uid === system.uid)) {
    return { session: name, socketPath: join(system.runtime_directory, name, "client.sock") };
  }
  const runtimeRoot = environment.XDG_RUNTIME_DIR || join(environment.TMPDIR || tmpdir(), `litebackup-${uid}`);
  return { session: name, socketPath: join(runtimeRoot, "litebackup", name, "client.sock") };
}
