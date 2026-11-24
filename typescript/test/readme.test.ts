// The README's code blocks must run as pasted into an empty directory.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const readme = readFileSync(new URL("../../README.md", import.meta.url), "utf8");
const blocks = [...readme.matchAll(/```ts\n([\s\S]*?)```/g)].map((match) => match[1]!);

test("README code blocks create what they use", () => {
  assert.ok(blocks.length > 0);
  for (const block of blocks) {
    for (const [, directory] of block.matchAll(/new (?:Database|DatabaseSync)\((?:path|"([^"]*)")/g)) {
      const opened = directory ?? /const path = "([^"]*)"/.exec(block)?.[1];
      assert.ok(opened, block);
      const parent = opened.includes("/") ? opened.slice(0, opened.lastIndexOf("/")) : undefined;
      if (parent) assert.match(block, new RegExp(`mkdirSync\\("${parent}", \\{ recursive: true \\}\\)`), block);
    }
    for (const [, table] of block.matchAll(/INSERT INTO (\w+)/g)) {
      assert.match(block, new RegExp(`CREATE TABLE (IF NOT EXISTS )?${table}\\b`), block);
    }
    for (const [, used] of block.matchAll(/\b(db|litebackup)\./g)) {
      assert.match(block, new RegExp(`const ${used} = |\\(${used}\\) =>`), `${used} is not defined in:\n${block}`);
    }
  }
});

test("README lists the packages its code blocks import", () => {
  const install = /npm install ([^`]*)`/.exec(readme)?.[1]?.split(" ") ?? [];
  for (const block of blocks) {
    for (const [, specifier] of block.matchAll(/from "([^"]+)"/g)) {
      if (specifier!.startsWith("node:")) continue;
      const name = specifier!.startsWith("@") ? specifier!.split("/").slice(0, 2).join("/") : specifier!.split("/")[0]!;
      assert.ok(install.includes(name), `${name} is missing from the install command`);
    }
  }
});
