// The TypeScript step of the cross-language handoff test, run against the built SDK.
// Usage: node typescript-step.mjs SDK-DIST NAME PATH EXPECTED-ROWS ADDED-ROWS
const [distribution, name, path, expected, added] = process.argv.slice(2);
const { Litebackup } = await import(`${distribution}/src/index.js`);
const nodeSqlite = await import(`${distribution}/src/node-sqlite.js`);
const litebackup = new Litebackup();
try {
  await litebackup.databases.managed(name, { to: path, durability: "server", adapter: nodeSqlite.adapter() }, (db) => {
    const { rows } = db.prepare("SELECT count(*) AS rows FROM steps").get();
    if (rows !== Number(expected)) throw new Error(`found ${rows} rows, want ${expected}`);
    db.exec("BEGIN");
    for (let index = 0; index < Number(added); index++) db.prepare("INSERT INTO steps (body) VALUES (?)").run(`typescript ${index}`);
    db.exec("COMMIT");
  });
  console.log("typescript handoff ok");
} finally {
  litebackup.close();
}
