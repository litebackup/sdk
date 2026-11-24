import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { ErrorCode, LitebackupError } from "../src/index.js";
import { currentUmask, requestUmask, statusUmask } from "../src/umask.js";

test("reads the process umask, parses a status file, and checks a given umask", async () => {
  assert.equal(currentUmask(), process.umask());
  const directory = await mkdtemp(join(tmpdir(), "lbu-"));
  const status = join(directory, "status");
  await writeFile(status, "Name:\tapp\nUmask:\t0077\nState:\tR\n");
  assert.equal(statusUmask(status), 0o077);
  await writeFile(status, Buffer.concat([Buffer.from("Name:\tapp"), Buffer.from([0xff]), Buffer.from("\nUmask:\t0022\n")]));
  assert.equal(statusUmask(status), 0o022);
  assert.equal(statusUmask(join(directory, "missing")), undefined);
  assert.equal(requestUmask(undefined), process.umask());
  assert.equal(requestUmask(0), 0);
  for (const umask of [-1, 0o1000, 1.5, true as unknown as number]) {
    assert.throws(() => requestUmask(umask), (error: unknown) => error instanceof LitebackupError && error.code === ErrorCode.invalidArgument);
  }
});

test("sends the umask the process has when the call is made", () => {
  const previous = process.umask(0o077);
  try {
    assert.equal(requestUmask(undefined), 0o077);
    process.umask(0o002);
    assert.equal(requestUmask(undefined), 0o002);
  } finally {
    process.umask(previous);
  }
});
