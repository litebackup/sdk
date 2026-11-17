import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { SignatureError, findRelease } from "../src/index.js";

// The fixtures are shared with the Go tests: the website's package metadata as
// Litebackup's release key signed it, and a copy whose 0.3.30 document has one
// SHA-256 changed under the original signature. Tests run from dist/test.
const testdata = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "testdata");
const fixture = (name: string) => readFileSync(join(testdata, name), "utf8");

test("verifies the signed package metadata", () => {
  const metadata = fixture("package-metadata.json");
  const release = findRelease(metadata);
  assert.equal(release.version, "0.3.30");
  assert.deepEqual(release.channels, ["stable"]);
  const file = release.files.find((candidate) => candidate.name === "litebackup-linux-amd64");
  assert.equal(file?.sha256, "3338a925ace256760fe48c1fb660c6ffdcc9be570fef5af7f40f8d34ae9391d7");
  assert.equal(file?.size, 47322128);
  assert.throws(() => findRelease(JSON.parse(metadata), { version: "0.3.1" }));
  assert.throws(() => findRelease(metadata, { keys: { "01a1ead6-4b94-79ca-81f4-3a67c112bc0b": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo" } }), SignatureError);
});

test("refuses the tampered copy", () => {
  const metadata = fixture("package-metadata-tampered.json");
  assert.throws(() => findRelease(metadata), SignatureError);
});
