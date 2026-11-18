import { createServer } from "node:http";
import { DatabaseSync } from "node:sqlite";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();

const db = new DatabaseSync("/data/main.sqlite");
db.exec("CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY)");

const server = createServer((request, response) => {
  try {
    if (request.method === "POST") db.exec("INSERT INTO visits DEFAULT VALUES");
    const { count } = db.prepare("SELECT count(*) AS count FROM visits").get();
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ visits: count }));
  } catch (error) {
    console.error(error);
    response.writeHead(500).end("Database request failed");
  }
});

await litebackup.backups.start("/data/main.sqlite");
server.listen(Number(process.env.PORT ?? 3000), "0.0.0.0");
process.on("SIGTERM", () => server.close(() => db.close()));
