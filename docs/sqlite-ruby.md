# SQLite backups from Ruby and Rails

The Ruby SDK talks to the litebackup client on the same machine, as the
Python and TypeScript SDKs do. It needs Ruby 3.2 or later on Linux or macOS
and has no runtime dependencies. Install it with `gem install litebackup`, or
`gem "litebackup"` in your `Gemfile`; see [`ruby/`](../ruby/README.md).

## Plain Ruby

```ruby
require "litebackup"

Litebackup::Client.open do |litebackup|
  litebackup.backups.start("/var/lib/app/main.sqlite")
  litebackup.databases.versions("main.sqlite").each { |version| puts version.version }
end
```

`Litebackup::Client.new` takes `api_key:`, `session:`, `socket_path:`,
`binary_path:`, `launch:`, and `environment:`, which the
[SDK reference](sdk-reference.md) describes. A client is safe to share
between threads. A client made before a fork, as in a Puma app that preloads,
raises `invalid_argument` in the forked process: make one there.

Durations are seconds, so `timeout: 90` and `timeout: 1.5` are both
accepted, and so is an `ActiveSupport::Duration` such as `5.minutes`. Times
are `Time`, and paths are a `String` or a `Pathname`. Listings and watches
without a block return an `Enumerator`:

```ruby
require "litebackup"

litebackup = Litebackup::Client.new
litebackup.events.watch(after: -1).lazy.select { |event| event.type == "released" }.first(3)
litebackup.close
```

## Leases with the sqlite3 gem

`Litebackup::Integrations::SQLite3.adapter` opens an existing file with the
`sqlite3` gem and never creates one; a missing file raises `not_found`. Its
options go to `SQLite3::Database.new`, such as `results_as_hash: true`.

```ruby
require "litebackup"
require "litebackup/integrations/sqlite3"

Litebackup::Client.open do |litebackup|
  litebackup.databases.pool(Litebackup::Integrations::SQLite3.adapter, idle_release: 300) do |pool|
    pool.lease("agents/42.sqlite", path: "data/agents/42.sqlite", durability: "cloud", offload: true) do |db|
      db.transaction do
        db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
        db.execute("INSERT INTO steps (body) VALUES (?)", ["tests passed"])
      end
    end
  end
end
```

The first lease of a name locks the database for this machine and opens it,
creating an empty file when none is there unless `require_existing: true`.
Later leases reuse the lock and the open database. The pool releases it after
`idle_release` seconds unused (a minute by default), on `pool.release(name)`,
or when the pool closes. With `durability`, a block that finishes returns once
the server, or with `"cloud"` the bucket, has what it committed.

A block that raises, or that the code leaves with `break` or `return`, counts
as unfinished: the lease stays, and its release never offloads the file.
`next` finishes the block. `pool.use(name)` is for a database a backup rule on
this machine covers; uses of one database share its handle across threads.

A close that fails never releases, because a connection could still be open:
the pool raises `Litebackup::LeaseCleanupError` with `step` `"close"`, and the
database stays locked for this machine until every connection is closed and
`pool.release(name)` succeeds. Several failures at once are a
`Litebackup::ErrorGroup`.

## Rails

```ruby
# config/application.rb
require "litebackup/integrations/rails"

module App
  class Application < Rails::Application
    config.litebackup.databases = {
      primary: {},
      queue: { name: "app/queue.sqlite" },
      cache: {},
      cable: {}
    }
  end
end
```

Each key of `databases` is a database name of the environment's
`config/database.yml`, replicas and hidden databases included. Its options
are those of `backups.start`: `name`, `prefix`, `maximum_local_databases`,
`offload_durability`, and `offload_rollback_journal`. `config.litebackup`
also takes the client's `api_key`, `session`, `socket_path`, and
`binary_path`.

| Setting | Default | What happens |
| --- | --- | --- |
| `databases` | none | Nothing is registered until a database is named |
| `enabled` | false in the test environment and in Rake tasks, true otherwise | The server, the console, and `bin/jobs` register the backups; `assets:precompile` and `db:migrate` do not |

After the app initializes, each named database's backup is started with a
client that closes once the rules are saved. Boot stops with
`Litebackup::Integrations::Railtie::ConfigurationError` for a name the
environment lacks, an adapter other than `sqlite3`, an in-memory or test
database, a file whose directory does not exist, an unknown key, or a backup
the daemon refuses. A database file that does not exist yet is created empty,
the database SQLite would create on first connect.

Registration runs once each time the app initializes: in Puma's master when
it preloads the app before forking workers (`preload_app!`), in each worker
when it does not, and in `bin/jobs`. The daemon returns the saved rule for the
same settings, so registering again is safe; changed settings raise `rule_exists` at boot until the old rule is
stopped with `litebackup.rules.stop(id)`.
