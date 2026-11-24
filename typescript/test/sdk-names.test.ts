// Checks the TypeScript SDK against testdata/contract/names/sdk-names.json,
// the names all three SDKs share.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { Litebackup } from "../src/index.js";

interface SDKNames {
  operations: { operation: string; go: string }[];
  types: string[];
}

const sdkNames = JSON.parse(
  readFileSync(new URL("../../../testdata/contract/names/sdk-names.json", import.meta.url), "utf8"),
) as SDKNames;
const indexSource = readFileSync(new URL("../../src/index.ts", import.meta.url), "utf8");

/** Writes a Python method name, such as start_prefix, in camelCase. */
function camelCase(name: string): string {
  return name.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
}

test("each shared operation is its group's method", () => {
  const litebackup = new Litebackup({ socketPath: "/nonexistent/client.sock" });
  const groups = litebackup as unknown as Record<string, Record<string, unknown>>;
  for (const { operation } of sdkNames.operations) {
    const [group, method] = operation.split(".") as [string, string];
    assert.equal(typeof groups[group]?.[camelCase(method)], "function", `${group}.${camelCase(method)}`);
  }
});

test("the package exports each shared type", () => {
  const exported = new Set(
    [...indexSource.matchAll(/export (?:type )?\{([^}]*)\}/g)].flatMap((match) =>
      match[1]!.split(",").map((name) => name.trim()).filter((name) => name !== ""),
    ),
  );
  for (const typeName of sdkNames.types) {
    assert.ok(exported.has(typeName), `the package exports no ${typeName}`);
  }
});
