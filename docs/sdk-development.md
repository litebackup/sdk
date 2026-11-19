# Developing the Litebackup SDK

This repository contains the TypeScript package, the Python package, the Go
module, and a copy of the client socket OpenAPI specification. Changes here do not publish an SDK or
modify a running server.

| Path | Purpose |
| --- | --- |
| `typescript/src` | TypeScript client, helpers, and driver subpath exports |
| `python/src/litebackup` | Python clients and `integrations` |
| `litebackup` | Go package; `sqlitedsn` resolves `database/sql` DSNs |
| `typescript/examples`, `examples` | Compiled examples: Drizzle, agent state, Django, FastAPI, a Go app, cron, Docker |
| `client-openapi.yaml` | The local client's OpenAPI specification |
| `docs/sdk-contract.md`, `testdata/contract` | The behavior every SDK shares, and the fixtures each SDK's tests run |

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
(cd litebackup/internal/drivertest && go test ./...)   # sqlitedsn and SQLAdapter against real drivers
```

```sh
cd python
uv sync
uv run pytest
uv run mypy src
uv run ruff check src tests && uv run ruff format --check src tests
```

Omitting optional dependencies is only for SDK development without platform
binaries. Normal consumer installs should retain them. To run the examples
without optional packages, set `LITEBACKUP_PATH` to an installed binary.

After updating `client-openapi.yaml`, regenerate the types: `npm run generate`
in `typescript/`, `go generate ./litebackup`, and `scripts/generate` in
`python/`. Generated names are the spec's; do not edit generated files.

A behavior change to any SDK starts in `docs/sdk-contract.md` and the
fixtures in `testdata/contract`; all three test suites run every fixture.

## Real-server integration tests

Use a compatible Litebackup binary, then run:

```sh
LITEBACKUP_INTEGRATION=1 LITEBACKUP_PATH=/absolute/path/to/litebackup \
  go test -timeout 20m ./litebackup
cd typescript
LITEBACKUP_INTEGRATION=1 LITEBACKUP_PATH=/absolute/path/to/litebackup npm test
cd ../python
LITEBACKUP_INTEGRATION=1 LITEBACKUP_PATH=/absolute/path/to/litebackup uv run pytest tests/test_integration.py
```

Set `LITEBACKUP_OLD_PATH` to a litebackup older than 1.0.0 to also check that
the Go SDK refuses it. The Python suite includes the cross-language handoff:
Python writes and releases, TypeScript and then Go acquire, read, and write.
It needs the TypeScript SDK built (`npm run build`).

Run the integration commands sequentially: the development servers share
the fixed local health port 8099. Tests start development servers with local
file buckets and isolated clients.
Go integration tests need the `sqlite3` command. They exercise backup, naming,
restore, replication, acquire/release, and conflict preservation.
Tests without the environment flag skip the real-server cases.

## Package checks

Both package builds take the `cli` release's files under their published
names (`litebackup-linux-amd64`, ...) and the package metadata from
`https://litebackup.com/pkg/metadata`. Each verifies the metadata's signature
and every file's SHA-256 and size before it copies anything.

```sh
cd typescript
npm run build
./scripts/stage-platforms /absolute/path/to/release /absolute/path/to/metadata.json
./scripts/pack-check
cd ../python
./scripts/build-wheels /absolute/path/to/release /absolute/path/to/metadata.json
```

`pack-check` packs the SDK and this platform's package, installs them in a
temporary consumer, runs the binary, and compiles every TypeScript example in
the docs. `build-wheels` writes an sdist and one wheel per platform tag to
`python/dist`; it reads each tag from the binary (static Linux:
`manylinux_2_17` and `musllinux_1_2`; dynamic: the newest `GLIBC_` version it
needs; macOS: the minimum OS in `LC_BUILD_VERSION`). Do not check generated
binaries or build output into the repository.

Package metadata signatures are checked against Litebackup's Ed25519 keys.
Every language tests both the valid fixture and a tampered fixture.

## Documentation changes

Keep normal app examples free of startup durability waits. Give finite jobs
a wait for the server or bucket and cleanup. Check CLI snippets against
`litebackup COMMAND --help`, compile complete TypeScript and Go examples,
and exercise examples touching files against a development server.
