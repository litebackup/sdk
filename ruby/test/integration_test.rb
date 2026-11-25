# frozen_string_literal: true

require_relative "test_helper"
require "litebackup/integrations/sqlite3"
require "socket"

# Against a real server and daemons: set LITEBACKUP_INTEGRATION=1 and
# LITEBACKUP_PATH to a static litebackup binary from scripts/build, as
# scripts/test --integration does.
module Integration
  ENABLED = ENV["LITEBACKUP_INTEGRATION"] == "1" && !ENV.fetch("LITEBACKUP_PATH", "").empty?

  # A litebackup server run --development with a local bucket, as the
  # Python, Go, and TypeScript suites use.
  class DevelopmentServer
    attr_reader :binary, :directory, :tag, :environment, :address

    def initialize
      @binary = ENV.fetch("LITEBACKUP_PATH")
      @directory = Dir.mktmpdir("lbrb")
      @tag = File.basename(@directory).downcase
      @server_directory = File.join(@directory, "server")
      probe = TCPServer.new("127.0.0.1", 0)
      @address = "127.0.0.1:#{probe.addr[1]}"
      probe.close
      @environment = ENV.to_h.merge(
        "XDG_RUNTIME_DIR" => File.join(@directory, "run"), "XDG_STATE_HOME" => File.join(@directory, "state"),
        "LITEBACKUP_API_KEY" => "", "LITEBACKUP_SESSION" => ""
      )
      @sessions = []
      @clients = []
      log = File.join(@directory, "server.log")
      @pid = Process.spawn(@environment, @binary, "server", "run", "--development",
                           "--data-directory", @server_directory,
                           "--listen-address", @address, "--archive-delay", "1s", "--minimum-sync-interval", "0s",
                           "--session-lease", "3s", "--session-heartbeat", "1s", out: log, err: log)
      Integration.eventually(30) { join_token("probe") }
      admin("bucket", "create", "--provider", "s3", "--endpoint", "file://#{File.join(@directory, "bucket")}",
            "--bucket", "bucket", "--name", "local")
      admin("setup", "complete", "key_backup")
      admin("setup", "complete", "retention")
    end

    def admin(*arguments)
      output, status = Open3.capture2e(@environment, @binary, "server", *arguments, "--data-directory",
                                       @server_directory)
      raise "litebackup server #{arguments.join(" ")} failed: #{output}" unless status.success?

      output
    end

    def join_token(name)
      created = admin("join-token", "create", "--name", name, "--stdout-format", "json")
      "#{JSON.parse(created).fetch("token")}@http://#{@address}"
    end

    # A session of this run; its name carries the tag, so stray daemons are found.
    def session(name)
      "#{name}-#{@tag}"
    end

    # A client of a new session that joins with its own join token, as an app on another machine would.
    def connect(name)
      session_name = session(name)
      @sessions << session_name
      client = Litebackup::Client.new(session: session_name, api_key: join_token(session_name),
                                      environment: @environment)
      client.connect
      @clients << client
      client
    end

    # A client of a session that joined before; its daemon keeps the join it made.
    def reconnect(name)
      client = Litebackup::Client.new(session: session(name), environment: @environment)
      client.connect
      @clients << client
      client
    end

    # The PIDs whose command line matches pattern, read from /proc.
    def pids(pattern)
      Dir["/proc/[0-9]*/cmdline"].filter_map do |cmdline|
        command = File.read(cmdline).tr("\0", " ").strip
        File.basename(File.dirname(cmdline)).to_i if pattern.match?(command)
      rescue SystemCallError
        nil
      end
    end

    # Stops a session's daemon with SIGKILL, so its lease runs out on the server.
    def kill(name)
      pids(/daemon --session #{Regexp.escape(session(name))}\z/).each { |pid| Process.kill(:KILL, pid) }
    end

    def path(*elements)
      path = File.join(@directory, *elements)
      FileUtils.mkdir_p(File.dirname(path))
      path
    end

    def close
      # Stop the daemons before the server, because their shutdown ends their sessions on it.
      @clients.each do |client|
        client.session.shutdown(durability: "none")
      rescue Litebackup::Error
        nil
      ensure
        client.close
      end
      pids(/#{Regexp.escape(@tag)}/).each do |pid|
        Process.kill(:KILL, pid)
      rescue Errno::ESRCH
        nil
      end
      Process.kill(:KILL, @pid)
      Process.wait(@pid)
      FileUtils.rm_rf(@directory)
    end
  end

  module_function

  # Calls the block until it stops raising or seconds pass.
  def eventually(seconds)
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + seconds
    begin
      yield
    rescue StandardError, Minitest::Assertion
      raise if Process.clock_gettime(Process::CLOCK_MONOTONIC) > deadline

      sleep(0.25)
      retry
    end
  end

  def add_items(path, count)
    FileUtils.mkdir_p(File.dirname(path))
    database = SQLite3::Database.new(path)
    database.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body BLOB)")
    count.times { database.execute("INSERT INTO items (body) VALUES (randomblob(2000))") }
    database.close
  end

  def count_items(path)
    database = SQLite3::Database.new(path, flags: SQLite3::Constants::Open::READONLY)
    database.get_first_value("SELECT count(*) FROM items")
  ensure
    database&.close
  end
end

class IntegrationTest < Minitest::Test
  include Integration

  def setup
    skip("set LITEBACKUP_INTEGRATION=1 and LITEBACKUP_PATH") unless Integration::ENABLED
    @server = DevelopmentServer.new
  end

  def teardown
    @server&.close
  end

  def test_backs_up_registers_again_restores_replicates_acquires_and_releases
    web1 = @server.connect("web-1")
    web2 = @server.connect("web-2")
    reader = @server.connect("reader")
    refute_empty(web1.client_version)

    main_path = @server.path("web-1", "main.db")
    add_items(main_path, 10)
    database = SQLite3::Database.new(main_path)
    rule = web1.backups.start(Litebackup::Integrations::SQLite3.database_path(database))
    database.close
    assert_equal("main.db", rule.name)
    assert_equal(rule.id, web1.backups.start(main_path).id)
    denied = assert_raises(Litebackup::Error) { web1.backups.start(main_path, maximum_local_databases: 4) }
    assert_equal(["rule_exists", rule.id], [denied.code, denied.details["rule"]["id"]])
    web1.databases.sync(rule.name)
    add_items(main_path, 5)
    synced = web1.databases.sync(rule.name)
    versions = web1.databases.versions("main.db")
    assert_operator(versions.length, :>=, 2)
    assert_equal(synced.synced_server.version, versions.last.version)
    restored_path = @server.path("restored", "main.db")
    restored = web2.databases.restore("main.db", path: restored_path, version: versions.first.version)
    assert_equal(versions.first.version, restored.version)
    assert_equal(10, count_items(restored_path))
    existing = assert_raises(Litebackup::Error) { web2.databases.restore("main.db", path: restored_path) }
    assert_equal("file_exists", existing.code)
    head_path = @server.path("restored", "head.db")
    web2.databases.restore("main.db", path: Pathname.new(head_path), time: Time.now)
    assert_equal(15, count_items(head_path))

    replica_path = @server.path("reader", "main.db")
    replica = reader.replicas.start("main.db", path: replica_path)
    assert_equal("replica", replica.role)
    assert_equal(15, count_items(replica_path))
    assert_equal(1, reader.replicas.list.replicas.length)

    tenant_path = @server.path("web-1", "42.db")
    add_items(tenant_path, 4)
    acquired = web1.databases.acquire("tenants/42.db", path: tenant_path, maintenance: true)
    assert_equal("created", acquired.decision)
    assert(web1.databases.writer("tenants/42.db").this_machine)
    add_items(tenant_path, 2)
    released = web1.databases.release("tenants/42.db", durability: "server")
    assert_operator(released.database.synced_server.version, :>=, 2)
    moved_path = @server.path("web-2", "42.db")
    assert_equal("downloaded", web2.databases.acquire("tenants/42.db", path: moved_path).decision)
    assert_equal(6, count_items(moved_path))
    add_items(moved_path, 1)
    web2.databases.release("tenants/42.db", durability: "server")

    # Writes the head never saw, made after web-2 moved the head on, diverge:
    # the copy is refused and left as it is, unless discard_local.
    add_items(tenant_path, 3)
    conflict = assert_raises(Litebackup::Error) { web1.databases.acquire("tenants/42.db", path: tenant_path) }
    assert_equal("conflict", conflict.code)
    assert_equal(9, count_items(tenant_path))
    replaced = web1.databases.acquire("tenants/42.db", path: tenant_path, discard_local: true)
    assert_equal("replaced", replaced.decision)
    assert_equal(7, count_items(tenant_path))
    web1.databases.release("tenants/42.db", durability: "server")
  end

  # A pool lease writes through the sqlite3 adapter, rolls back what it did
  # not commit, and offloads on release once the bucket has the version.
  def test_a_pool_leases_a_database_and_offloads_it_on_release
    worker = @server.connect("worker")
    path = @server.path("worker", "7.sqlite")
    worker.databases.pool(Litebackup::Integrations::SQLite3.adapter) do |pool|
      pool.lease("agents/7.sqlite", path: path, durability: "cloud", offload: true) do |db|
        db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, body BLOB)")
        3.times { db.execute("INSERT INTO items (body) VALUES ('ruby')") }
        db.transaction
        db.execute("INSERT INTO items (body) VALUES ('never committed')")
      end
    end
    status = worker.databases.get("agents/7.sqlite")
    assert_equal("offloaded", status.state)
    assert_equal(status.synced_server.version, status.synced_cloud.version)
    back = @server.path("back", "7.sqlite")
    worker.databases.restore("agents/7.sqlite", path: back)
    assert_equal(3, count_items(back))
  end

  # A close that fails never releases: the database stays acquired until
  # every connection is closed and release is called.
  def test_a_close_failure_keeps_the_database_acquired
    worker = @server.connect("close")
    path = @server.path("close", "9.sqlite")
    leaky = Class.new do
      include Litebackup::Adapter

      def open(path) = SQLite3::Database.new(path, flags: SQLite3::Constants::Open::READWRITE)
      def close(_handle) = raise("a pooled connection is still in use")
    end
    pool = worker.databases.pool(leaky.new)
    leaked = pool.lease("agents/9.sqlite", path: path, durability: "server") do |db|
      db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, body BLOB)")
      db
    end
    error = assert_raises(Litebackup::LeaseCleanupError) { pool.close }
    assert_equal(%w[lease_cleanup_failed close], [error.code, error.step])
    assert_equal({ "step" => "close", "name" => "agents/9.sqlite", "path" => path, "acquired" => true }, error.details)
    assert(worker.databases.writer("agents/9.sqlite").this_machine)
    leaked.close
    worker.databases.release("agents/9.sqlite", durability: "server")
    refute(worker.databases.writer("agents/9.sqlite").this_machine)
  end

  # The origin dies and the replica takes over within seconds of the 3 s
  # lease; the old origin comes back with writes the server lacks and is
  # refused, its file left as it is.
  def test_fails_over_and_denies_the_old_origins_diverged_file
    first = @server.connect("first")
    second = @server.connect("second")
    first_path = @server.path("first", "shared.db")
    add_items(first_path, 5)
    first_roles = Queue.new
    first_failover = first.failovers.start(first_path, name: "shared.db", on_role_change: lambda { |role|
      first_roles << role
    })
    assert(first_failover.origin?)
    first.databases.sync("shared.db")
    second_path = @server.path("second", "shared.db")
    promoted = Gate.new
    second_failover = second.failovers.start(second_path, name: "shared.db",
                                                          on_role_change: lambda { |role|
                                                            promoted.open! if role == "origin"
                                                          })
    assert_equal("replica", second_failover.role)
    eventually(30) { assert_equal(5, count_items(second_path)) }

    @server.kill("first")
    killed = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    # The old origin's follower loses its daemon, so its role is unknown at once.
    eventually(2) { assert_equal("unknown", first_failover.role) }
    assert(promoted.wait(30), "the replica never took over")
    assert_operator(Process.clock_gettime(Process::CLOCK_MONOTONIC) - killed, :<, 6)
    assert(second_failover.origin?)
    # The old origin's app kept writing while its daemon was gone.
    add_items(first_path, 3)

    restarted = @server.reconnect("first")
    begin
      restarted.failovers.start(first_path, name: "shared.db")
    rescue Litebackup::Error => e
      raise unless e.code == "conflict"
    end
    eventually(60) do
      denied = restarted.events.page(limit: 10_000).events.find do |event|
        event.type == "denied" && event.name == "shared.db" && event.path == first_path
      end
      refute_nil(denied, "no denied event naming the file yet")
    end
    assert_equal(8, count_items(first_path))
    assert(second_failover.origin?)
    second_failover.stop
  end
end
