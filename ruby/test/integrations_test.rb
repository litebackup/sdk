# frozen_string_literal: true

require_relative "test_helper"
require "litebackup/integrations/sqlite3"

class SQLite3IntegrationTest < Minitest::Test
  include WorkingDirectory

  SQLite3Integration = Litebackup::Integrations::SQLite3
  SQLiteFile = Litebackup::Integrations::SQLiteFile

  def test_database_path_reads_the_file_a_database_has_open
    database = SQLite3::Database.new("app.sqlite")
    assert_equal(File.join(@working_directory, "app.sqlite"), SQLite3Integration.database_path(database))
    database.execute("ATTACH DATABASE 'second.sqlite' AS second")
    assert_equal(File.join(@working_directory, "second.sqlite"), SQLite3Integration.database_path(database, "second"))
    assert_equal("invalid_argument", assert_raises(Litebackup::Error) do
      SQLite3Integration.database_path(database, "x")
    end.code)
    database.close
    hashes = SQLite3::Database.new("app.sqlite", results_as_hash: true)
    assert_equal(File.join(@working_directory, "app.sqlite"), SQLite3Integration.database_path(hashes))
    hashes.close
    memory = SQLite3::Database.new(":memory:")
    assert_equal("invalid_argument", assert_raises(Litebackup::Error) { SQLite3Integration.database_path(memory) }.code)
    memory.close
  end

  def test_the_adapter_opens_existing_files_only
    adapter = SQLite3Integration.adapter
    error = assert_raises(Litebackup::Error) { adapter.open(File.join(@working_directory, "missing.sqlite")) }
    assert_equal("not_found", error.code)
    refute_path_exists("missing.sqlite")
    File.write("empty.sqlite", "")
    database = adapter.open(File.join(@working_directory, "empty.sqlite"))
    database.execute("CREATE TABLE steps (body TEXT)")
    adapter.close(database)
    assert(database.closed?)
  end

  def test_the_adapter_rolls_back_and_never_commits
    adapter = SQLite3Integration.adapter
    File.write("app.sqlite", "")
    database = adapter.open(File.join(@working_directory, "app.sqlite"))
    database.execute("CREATE TABLE steps (body TEXT)")
    database.transaction
    database.execute("INSERT INTO steps (body) VALUES ('unfinished')")
    adapter.close(database)
    reopened = SQLite3::Database.new("app.sqlite")
    assert_equal(0, reopened.get_first_value("SELECT count(*) FROM steps"))
    reopened.close
  end

  def test_the_adapter_serves_a_pool_lease
    daemon = LeaseDaemonStandIn.new(File.join(@working_directory, "agents", "7.sqlite"))
    Litebackup::Client.open(socket_path: daemon.socket_path) do |client|
      client.databases.pool(SQLite3Integration.adapter) do |pool|
        pool.lease("agents/7.sqlite", path: "agents/7.sqlite") do |db|
          db.execute("CREATE TABLE steps (body TEXT)")
          db.execute("INSERT INTO steps (body) VALUES (?)", ["tests passed"])
        end
        count = pool.lease("agents/7.sqlite", path: "agents/7.sqlite") do |db|
          db.get_first_value("SELECT count(*) FROM steps")
        end
        assert_equal(1, count)
      end
    end
    assert_equal(%w[acquire status release], daemon.requests.map { |received| received.path.split("/").last })
  ensure
    daemon&.close
  end

  def test_sqlite_filenames_and_uris
    assert_equal(File.join(@working_directory, "a b.sqlite"),
                 SQLiteFile.database_file("file:a%20b.sqlite?cache=shared"))
    assert_equal("/data/a+b.sqlite", SQLiteFile.database_file("file:///data/a+b.sqlite"))
    assert_equal("/data/a.sqlite", SQLiteFile.database_file("file://localhost/data/a.sqlite"))
    assert_equal("/root/db/a.sqlite", SQLiteFile.database_file("db/a.sqlite", "/root"))
    ["", ":memory:", "file:x?mode=memory", "file:x?vfs=memdb", "file://host/x"].each do |filename|
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) do
        SQLiteFile.database_file(filename)
      end.code, filename)
    end
  end
end

# A daemon that acquires and releases one database, for the adapter's lease.
class LeaseDaemonStandIn < ContractDaemon
  def initialize(path)
    status = { "name" => "agents/7.sqlite", "path" => path, "role" => "origin", "state" => "synced",
               "acquired" => true }
    super() do |received|
      case received.path
      when "/v1/databases/acquire" then json_answer("database" => status, "decision" => "created")
      when "/v1/databases/release" then json_answer("database" => status, "offloaded" => false)
      else json_answer(status)
      end
    end
  end
end
