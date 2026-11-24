import { Litebackup } from "../src/index.js";

const litebackup = new Litebackup();

/** Reserve scheduled/ for these files. On fresh workers with a join token in LITEBACKUP_API_KEY, each path uses the token once. */
export async function backupFiles(paths: string[]): Promise<void> {
  if (paths.length === 0) throw new Error("Provide at least one SQLite path");
  for (const path of paths) {
    await litebackup.backups.once(path, { prefix: "scheduled/", timeout: "30m" });
    console.log(`Backed up ${path} to your server`);
  }
}
