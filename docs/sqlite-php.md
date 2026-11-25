# SQLite backups from PHP, Laravel, and Symfony

The PHP SDK talks to the litebackup client on the same machine, as the other
SDKs do. It needs PHP 8.2 or later on Linux or macOS with `ext-json`; the PDO
adapter needs `ext-pdo_sqlite`, the SQLite3 adapter `ext-sqlite3`, and release
verification `ext-sodium`. Install the Composer package `litebackup/sdk` from
Packagist with `composer require litebackup/sdk`.

The package bundles no `litebackup` binary, because Composer has no platform
packages. The SDK uses `LITEBACKUP_PATH`, then `litebackup` on `PATH`, then
`~/.local/bin/litebackup`; install it with
`curl -fsSL https://litebackup.com/cli.sh | bash`.

## Plain PHP

```php
<?php

require __DIR__ . '/vendor/autoload.php';

use Litebackup\Client;
use Litebackup\Integrations\PdoSqlite;

$db = new PDO('sqlite:/var/lib/app/main.sqlite');
$litebackup = new Client();
try {
    $rule = $litebackup->backups->start(PdoSqlite::databasePath($db));
    echo $rule->id, ' ', $rule->name, PHP_EOL;
    foreach ($litebackup->databases->versions('main.sqlite') as $version) {
        echo $version->version, ' ', $version->time?->format(DATE_ATOM), PHP_EOL;
    }
} finally {
    $litebackup->close();
}
```

`new Client()` takes `apiKey:`, `session:`, `socketPath:`, `binaryPath:`,
`launch:`, and `environment:`, which the [SDK reference](sdk-reference.md)
describes. `close()` closes the client's connections and never stops the
daemon. A client belongs to the PHP process that made it: one made before a
fork, as with `pcntl_fork`, is refused in the child with `invalid_argument`,
so make one there.

`backups->start()` returns the saved rule when the same path was registered
with the same settings, so call it on every start. Different settings for a
saved path raise `rule_exists`, with the saved rule in
`$error->details['rule']`.

Durations are seconds, so `timeout: 90` and `timeout: 1.5` are both
accepted. Times are `DateTimeImmutable`, and paths are a string, an
`SplFileInfo`, or any `Stringable`. Results are readonly objects with
camelCase properties, such as `$status->syncedServer->version`.
`databases->all()`, `databases->allVersions()`, and `events->watch()` return
a `Generator`:

```php
foreach ($litebackup->events->watch(after: -1) as $event) {
    if ($event->type === 'released') {
        echo $event->name, ' was released', PHP_EOL;
    }
}
```

## Restore

```php
$restored = $litebackup->databases->restore(
    'main.sqlite',
    path: '/var/lib/app/main-restored.sqlite',
    time: new DateTimeImmutable('-1 hour'),
);
echo $restored->version, PHP_EOL;
```

A restore writes a new file and checks it; an existing file is never
replaced and raises `file_exists`. Pass `version:` instead of `time:` for an
exact version.

## Leases with a pool

A pool keeps databases open between calls in this PHP process.
`Litebackup\Integrations\PdoSqlite::adapter()` opens an existing file with
PDO, and `Litebackup\Integrations\Sqlite3::adapter()` with the SQLite3
extension; neither creates one, and a missing file raises `not_found`. The
PDO adapter takes PDO attributes, such as
`[PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]`, and refuses persistent
connections.

```php
<?php

require __DIR__ . '/vendor/autoload.php';

use Litebackup\Client;
use Litebackup\Integrations\PdoSqlite;

$litebackup = new Client();
$pool = $litebackup->databases->pool(PdoSqlite::adapter(), idleRelease: 300);
try {
    $pool->lease('agents/42.sqlite', function (PDO $db): void {
        $db->beginTransaction();
        $db->exec('CREATE TABLE IF NOT EXISTS steps (body TEXT)');
        $db->prepare('INSERT INTO steps (body) VALUES (?)')->execute(['tests passed']);
        $db->commit();
    }, path: 'data/agents/42.sqlite', durability: 'cloud', offload: true);
} finally {
    $pool->close();
    $litebackup->close();
}
```

The first lease of a name locks the database for this machine and opens it,
creating an empty file when none is there unless `requireExisting: true`.
Later leases reuse the lock and the open database. `lockTimeout:` is how long
the first lease waits while another machine has the write lock; without it
the lease fails at once with `lock_unavailable`. With `durability`, work that
returns comes back once the server, or with `'cloud'` the bucket, has what it
committed. `$pool->use($name, $work)` is for a database a backup rule on this
machine covers.

The pool releases a lease after `idleRelease` seconds unused (a minute by
default), on `$pool->release($name)`, or on `$pool->close()`. Closing rolls
back a transaction the work left open, never commits it, and closes the
database; the release then syncs a final time, and with `offload` removes the
file. Work that throws counts as unfinished: the lease stays, and its release
never offloads the file.

PDO has no `close()`. The adapter checks that nothing else holds the
connection when the pool closes it, so a `PDOStatement` or a copy of the
connection that the work keeps, in a property or a static variable, keeps the
file open: the close fails, and the lease stays acquired. The pool raises
`Litebackup\LeaseCleanupError` with `step` `close`, and the database stays
locked for this machine until the statement is gone and `$pool->release($name)`
succeeds. Several failures at once are a `Litebackup\ErrorGroup`, whose
`errors` hold them.

PHP has no threads, so the idle close and the idle release are cooperative:
they run at the start of each pool call and in `close()`. A worker that makes
no call keeps its lease until it calls, closes, or exits; release a database
another machine waits for with `$pool->release($name)`. Under PHP-FPM a pool
lasts its request, and its leases are released when the request ends. When
`exit` or a fatal error ends the process without `close()`, a shutdown
function closes the pool and releases a lease whose call never finished
without offload. SIGTERM and SIGKILL run no shutdown function, and the daemon
keeps renewing its session, so a lease a killed PHP process held stays
acquired: release it with `litebackup release NAME`, with
`$litebackup->databases->release('NAME')` from another process, or by
shutting the session down. After an execution-time fatal error, as a time
limit raises, PHP runs the shutdown function while the work still holds its
PDO connection, so the PDO adapter's close fails and the lease stays
acquired the same way; the SQLite3 adapter closes its database itself and
releases it.

## Failovers

```php
use Litebackup\Failover;

$failover = $litebackup->failovers->start(
    '/var/lib/app/main.sqlite',
    onRoleChange: static function (string $role): void {
        error_log('this machine is now ' . $role);
    },
);
while (!$failover->stopped()) {
    $failover->follow(seconds: 5);
    if ($failover->isOrigin()) {
        // your code: write one batch, well under 15 seconds
    }
}
```

`follow(seconds)` reads the daemon's events in the calling process and runs
the callbacks there; the first one first passes `onRoleChange` the role the
start answered. Nothing follows the failover outside it, so `role()` reports
the last confirmed role for 15 seconds after its confirmation and
`Failover::UNKNOWN` after that, and `isOrigin()` turns false. A process that
writes only while `isOrigin()` holds never writes as a stale origin.
`onDenied` hears the daemon's message when it refused this machine's file,
and `$failover->stop()` stops the failover and releases its write lock.

## Laravel

Laravel registers the service provider from the package's `composer.json`.
Publish the settings and name the connections to back up:

```sh
php artisan vendor:publish --tag=litebackup-config
```

```php
// config/litebackup.php
return [
    'connections' => [
        'sqlite' => [],
        'queue' => ['name' => 'app/queue.sqlite'],
    ],
];
```

Each key of `connections` is a connection of `config/database.php`, its
`url` and its `read` and `write` variants included; a relative database path
resolves against the app's base path. Its options are those of
`backups->start()`, in snake_case: `name`, `prefix`,
`maximum_local_databases`, `offload_durability`, and
`offload_rollback_journal`. The file also takes the client's `api_key`,
`session`, `socket_path`, and `binary_path`.

| Setting | Default | What happens |
| --- | --- | --- |
| `connections` | none | Nothing is registered until a connection is named |
| `enabled` | yes, except in the testing environment | `queue:work`, `horizon:work`, `schedule:work`, and Octane workers register the backups when they start |

Run `php artisan litebackup:start` at every deploy: it registers each named
connection's database and prints its path. Workers register once when they
start, each Horizon child and each Octane worker boot included; `queue:listen`
registers in each job's process. HTTP requests, migrations, and other
commands never call the daemon.

A database file that does not exist yet is created empty, the database
SQLite would create on first connect. Registration stops with
`Litebackup\Integrations\ConfigurationError` for a name that is no
connection, a driver other than `sqlite`, an in-memory database, a file whose
directory does not exist, an unknown key, the testing environment, or a
backup the daemon refuses. The same settings register again safely; changed
settings raise `rule_exists` until the old rule is stopped with
`$litebackup->rules->stop($id)`.

## Symfony

Register the bundle, which needs DoctrineBundle, in `config/bundles.php`:

```php
return [
    // your bundles
    Litebackup\Integrations\Symfony\LitebackupBundle::class => ['all' => true],
];
```

Name the Doctrine DBAL connections to back up in
`config/packages/litebackup.yaml`:

```yaml
litebackup:
    connections:
        default: ~
        queue: { name: app/queue.sqlite }
```

Each connection must use the `pdo_sqlite` or `sqlite3` driver and a file; a
relative path resolves against the working directory, as SQLite opens it. The
options and the client settings are those of Laravel above, and `enabled` is
false in the test environment and true otherwise; while it is false,
`litebackup:start` registers nothing.

Run `bin/console litebackup:start` at every deploy: it registers each named
connection's database and prints its path. Each `messenger:consume` worker
registers once before it consumes its first message, and stops with the
`ConfigurationError` when registration fails. The bundle does nothing at
boot: HTTP requests, `cache:clear`, `doctrine:migrations:migrate`, and other
commands never call the daemon.

## Errors

Every failure is a `Litebackup\LitebackupError` with `errorCode`, the
message, `status`, and `details`. The code is `errorCode` because PHP's
`getCode()` is an integer; `describe()` gives the code and the message as one
line, and `getPrevious()` the underlying error. `Litebackup\ErrorCode` names
the codes worth matching:

```php
use Litebackup\ErrorCode;
use Litebackup\LitebackupError;

try {
    $litebackup->databases->acquire('agents/42.sqlite', path: 'data/agents/42.sqlite');
} catch (LitebackupError $error) {
    if ($error->errorCode !== ErrorCode::LOCK_UNAVAILABLE) {
        throw $error;
    }
    error_log('another machine writes agents/42.sqlite; try again later');
}
```

[Troubleshooting](sqlite-backup-errors.md) lists what each code means and what
to do.
