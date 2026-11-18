# Back up an agent fleet

On the server, create a token for 20 agents rolling out over the next day.

```sh
litebackup server join-token create --name agents --namespace agents --uses 20 --expires-in 24h
```

Distribute the token with the server address as `LITEBACKUP_TOKEN`. Each machine
needs its own persistent client state directory. Start a rule on each machine
with a distinct remote name in the token's namespace.

```ts
import { hostname } from "node:os";
import { Litebackup } from "@litebackup/sdk";

const litebackup = new Litebackup();
await litebackup.backups.start("/data/agent.sqlite", { name: hostname() + ".sqlite" });
```

Each first join consumes one token use and one available machine slot on the
instance plan. Each machine keeps an independently revocable identity. Restarting
with the saved session reuses that identity. Token expiry ends new joins.
Remove a machine in the console to revoke its access.
