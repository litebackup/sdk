# Developing the Litebackup SDK

This repository contains the TypeScript package, Go module, and a copy of the
client socket OpenAPI specification. Changes here do not publish an SDK or
modify a running server.

| Path | Purpose |
| --- | --- |
| `typescript/src` | TypeScript client and helpers |
| `litebackup` | Go package |
| `typescript/examples` | Runnable, compiled one-shot example |
| `examples/cron` | CLI script and example job image |
| `client-openapi.yaml` | The local client's OpenAPI specification |
| `testdata` | Signed and tampered package metadata fixtures |

## Build and test

```sh
cd typescript
npm ci --omit=optional
npm run generate
npm test
```

```sh
go vet ./...
go test ./...
```

Omitting optional dependencies is only for SDK development without platform
binaries. Normal consumer installs should retain them. To run the examples
without optional packages, set `LITEBACKUP_PATH` to an installed binary.

After updating `client-openapi.yaml`, run `npm run generate` in `typescript/`
to regenerate the API types.

## Real-server integration tests

Use a compatible Litebackup binary, then run:

```sh
LITEBACKUP_INTEGRATION=1 LITEBACKUP_PATH=/absolute/path/to/litebackup \
  go test -timeout 20m ./litebackup
cd typescript
LITEBACKUP_INTEGRATION=1 LITEBACKUP_PATH=/absolute/path/to/litebackup npm test
```

Run the two integration commands sequentially: the development servers share
the fixed local health port 8099. Tests start development servers with local
file buckets and isolated clients.
Go integration tests need the `sqlite3` command. They exercise backup, naming,
restore, replication, acquire/release, and conflict preservation.
Tests without the environment flag skip the real-server cases.

## Package checks

With compatible Litebackup release archives available locally:

```sh
cd typescript
./scripts/stage-platforms /absolute/path/to/release-archives
./scripts/pack-check
```

The check packs the SDK and platform packages, installs them in a temporary
consumer, and runs this platform's binary. Do not check generated binaries or
build output into the repository.

Package metadata signatures are checked against Litebackup's Ed25519 keys.
Every language tests both the valid fixture and a tampered fixture.

## Documentation changes

Keep normal app examples free of startup durability waits. Give finite jobs
a wait for the server or bucket and cleanup. Check CLI snippets against
`litebackup COMMAND --help`, compile complete TypeScript and Go examples,
and exercise examples touching files against a development server.
