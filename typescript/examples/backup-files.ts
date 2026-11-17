import { backup } from "../src/index.js";

/** Back up files not covered by a continuous backup. Reserve scheduled/ for this job. */
export async function backupFiles(paths: string[]): Promise<void> {
  if (paths.length === 0) throw new Error("Provide at least one SQLite path");
  for (const path of paths) {
    const run = backup(path, { prefix: "scheduled/" });
    await run.ready;
    try {
      await run.wait("cloud", { signal: AbortSignal.timeout(30 * 60_000) });
    } finally {
      await run.stop();
    }
    console.log(`Backed up ${run.name} to cloud`);
  }
}
