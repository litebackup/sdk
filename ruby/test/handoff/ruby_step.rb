# frozen_string_literal: true

# The Ruby step of the cross-language handoff test, which
# sdk/python/tests/test_integration.py runs through scripts/ruby-in-docker
# when LITEBACKUP_RUBY names a command.
#
# Usage: ruby ruby_step.rb NAME PATH EXPECTED-ROWS ADDED-ROWS
#
# It leases NAME at PATH in the session LITEBACKUP_SESSION, checks that the
# database holds EXPECTED-ROWS rows, adds ADDED-ROWS, and closes the pool,
# which releases the database. Its daemon runs in this container, so the
# step shuts the session down before it exits: the next step's machine
# acquires the database from the server.

require "litebackup"
require "litebackup/integrations/sqlite3"

name, path, expected, added = ARGV
client = Litebackup::Client.new
begin
  client.databases.pool(Litebackup::Integrations::SQLite3.adapter) do |pool|
    pool.lease(name, path: path, durability: "server") do |db|
      step_count = db.get_first_value("SELECT count(*) FROM steps")
      raise "found #{step_count} rows, want #{expected}" unless step_count == Integer(expected)

      db.transaction do
        Integer(added).times { |index| db.execute("INSERT INTO steps (body) VALUES (?)", ["ruby #{index}"]) }
      end
    end
  end
  puts("ruby handoff ok")
ensure
  begin
    shutdown = client.session.shutdown(durability: "server")
    warn("the session kept #{shutdown.failed.map(&:name).join(", ")}") unless shutdown.failed.empty?
  ensure
    client.close
  end
end
