import { backupFiles } from "./backup-files.js";

try {
  await backupFiles(process.argv.slice(2));
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
