import { Litebackup } from "../src/index.js";

const litebackup = new Litebackup();

/** Back up files not covered by a continuous backup. Reserve scheduled/ for this job. */
export async function backupFiles(paths: string[]): Promise<void> {
  if (paths.length === 0) throw new Error("Provide at least one SQLite path");
  for (const path of paths) {
    await litebackup.backups.once(path, { prefix: "scheduled/", wait: "cloud", timeout: "30m" });
    console.log(`Backed up ${path} to cloud`);
  }
}
