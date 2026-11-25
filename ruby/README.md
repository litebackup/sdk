# Litebackup for Ruby

Back up, replicate, and restore SQLite databases with your Litebackup server
and bucket, from Ruby and Rails. Ruby 3.2 or later on Linux or macOS. The gem
has no runtime dependencies; the sqlite3 adapter and the Rails integration
load those gems when you require them.

Install the gem from RubyGems, or add it to your `Gemfile`:

```sh
gem install litebackup
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
```

`LITEBACKUP_API_KEY` is the value from **Machines → Add machine**, or an API
key followed by @ and your server address. RubyGems and Bundler install the
platform gem for Linux (glibc and musl) or macOS, on x64 or arm64, which
carries the `litebackup` command without putting it on `PATH`. Elsewhere the
plain gem uses an installed `litebackup` command: install it with
`curl -fsSL https://litebackup.com/cli.sh | bash`, or set `LITEBACKUP_PATH`.

## Back up an app's database

```ruby
require "litebackup"
require "litebackup/integrations/sqlite3"

FileUtils.mkdir_p("data")
database = SQLite3::Database.new("data/app.sqlite")
Litebackup::Client.open do |litebackup|
  rule = litebackup.backups.start(Litebackup::Integrations::SQLite3.database_path(database))
  puts "#{rule.id} #{rule.name}"
end
```

`backups.start` returns the saved rule when the same path was registered
with the same settings, so call it on every start. Different settings for a
saved path raise `rule_exists`, with the saved rule in `error.details["rule"]`.

## Rails

Name the databases to back up in `config/application.rb`. Each is a database
of the environment's `config/database.yml`, the Solid Queue, Cache, and Cable
databases included:

```ruby
require "litebackup/integrations/rails"

module App
  class Application < Rails::Application
    config.litebackup.databases = { primary: {}, queue: {}, cache: {}, cable: {} }
  end
end
```

The backups are registered after the app initializes, in every process that
boots it, with the same settings each time. Registration is off in the test
environment and in Rake tasks such as `assets:precompile` and `db:migrate`;
set `config.litebackup.enabled` to choose. The [Ruby guide](../docs/sqlite-ruby.md)
has the options.

## Move a database between machines

A pool keeps a database open between calls. A lease locks the database for
this machine, so no other machine writes it, and keeps the lock until the
pool releases it:

```ruby
require "litebackup"
require "litebackup/integrations/sqlite3"

Litebackup::Client.open do |litebackup|
  litebackup.databases.pool(Litebackup::Integrations::SQLite3.adapter) do |pool|
    pool.lease("agents/42.sqlite", path: "data/agents/42.sqlite", durability: "server") do |db|
      db.execute("CREATE TABLE IF NOT EXISTS steps (body TEXT)")
      db.execute("INSERT INTO steps (body) VALUES (?)", ["tests passed"])
    end
  end
end
```

The adapter never commits: SQLite commits each statement outside a
transaction, and a transaction the block leaves open is rolled back when the
pool closes the database. A block that raises, or that it leaves with
`break` or `return`, keeps the lease and never offloads.

## Errors

Every failure is a `Litebackup::Error` with `code`, `message`, `status`, and
`details`. `Litebackup::ErrorCode` names the codes worth matching:

```ruby
begin
  litebackup.databases.acquire("agents/42.sqlite", path: "data/agents/42.sqlite")
rescue Litebackup::Error => e
  raise unless e.code == Litebackup::ErrorCode::LOCK_UNAVAILABLE

  warn("another machine writes agents/42.sqlite; try again later")
end
```

## Development

`scripts/test` runs the tests and rubocop in `ruby:3.2.11-slim`,
`ruby:3.3.14-slim`, and `ruby:3.4.13-slim` as the calling user;
`scripts/test generate` regenerates `lib/litebackup/generated/models.rb` from
`../client-openapi.yaml`. The [SDK reference](../docs/sdk-reference.md) lists
every method, and [the contract](../docs/sdk-contract.md) the behavior the
Go, TypeScript, Python, and Ruby SDKs share.
