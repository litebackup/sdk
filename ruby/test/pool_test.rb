# frozen_string_literal: true

require_relative "test_helper"
require "timeout"

# Pool behavior the lifecycle fixtures do not cover: kept locks, lost locks,
# incomplete blocks, idle close and release, closing, and forks. They mirror
# the Python SDK's test_pool.py.
LEASED_NAME = "agents/7.sqlite"
RULE_NAME = "tenants/42.sqlite"

# The status of a database this machine acquired.
def acquired_status(path)
  { "name" => LEASED_NAME, "path" => path, "role" => "origin", "state" => "synced", "acquired" => true,
    "failover" => false, "local_changed" => false, "synced_server" => { "version" => 3 },
    "synced_cloud" => { "version" => 3 }, "journal_mode" => "wal", "page_count" => 2, "sync_count" => 3,
    "hint_misses" => 0 }
end

# The status of a backup rule's database.
def rule_status(path, database_state)
  acquired_status(path).merge("name" => RULE_NAME, "acquired" => false, "state" => database_state,
                              "backup_rule_id" => "rule-1")
end

def journal_rule(path, offload_rollback_journal)
  { "id" => "rule-1", "path" => path, "name" => RULE_NAME, "prefix" => "", "watch" => "inotify",
    "maximum_local_databases" => 5, "offload_rollback_journal" => offload_rollback_journal, "failover" => false }
end

# Answers a pool's requests for one database: saves each database request as
# a step and keeps the database's status for status reads.
class LeaseDaemon
  attr_reader :steps, :status, :failing, :lock, :daemon

  def initialize(status, rules = [])
    @steps = []
    @status = status
    @rules = rules
    @failing = []
    @lock = Mutex.new
    @daemon = ContractDaemon.new { |received| answer(received) }
  end

  def answer(received)
    if received.path == "/v1/backup-rules/list"
      @steps << "rules"
      return json_answer("rules" => @rules)
    end
    step = received.path.delete_prefix("/v1/databases/")
    @steps << step
    return json_answer({ "code" => "unavailable", "message" => "#{step} failed" }, 503) if @failing.include?(step)

    @lock.synchronize do
      case step
      when "acquire"
        @status["state"] = "syncing"
        json_answer("database" => @status, "decision" => "downloaded")
      when "release"
        json_answer("database" => @status, "checkpointed" => false, "vacuumed" => false, "offloaded" => false)
      else json_answer(@status)
      end
    end
  end

  def request_bodies(path)
    daemon.requests.select { |received| received.path == path }.map(&:json)
  end
end

# Opens and closes stand-in handles, saving each as a step.
class PoolStepAdapter
  include Litebackup::Adapter

  def initialize(steps, close_seconds: 0, failing_closes: 0)
    @steps = steps
    @close_seconds = close_seconds
    @failing_closes = failing_closes
  end

  def open(_path)
    @steps << "open"
    "db"
  end

  def close(_handle)
    sleep(@close_seconds)
    @steps << "close"
    return unless @failing_closes.positive?

    @failing_closes -= 1
    raise "a connection is still open"
  end
end

class PoolTest < Minitest::Test
  include WorkingDirectory

  def setup
    super
    @daemons = []
  end

  def teardown
    @daemons.each { |daemon| daemon.daemon.close }
    Litebackup::Pool.check_seconds = 15.0
    Litebackup::Pool.offload_idle_close_seconds = Litebackup::Pool::DEFAULT_OFFLOAD_IDLE_CLOSE
    super
  end

  def lease_daemon(status, rules = [])
    daemon = LeaseDaemon.new(status, rules)
    @daemons << daemon
    daemon
  end

  def entries(pool)
    pool.instance_variable_get(:@entries)
  end

  def with_pool(daemon, adapter = PoolStepAdapter.new(daemon.steps), **options, &block)
    Litebackup::Client.open(socket_path: daemon.daemon.socket_path) do |client|
      client.databases.pool(adapter, **options) { |pool| block.call(pool, client) }
    end
  end

  def path
    File.join(@working_directory, "7.sqlite")
  end

  # Only the first lease acquires and opens, a lease with durability syncs
  # without releasing, and close closes and releases once.
  def test_lease_keeps_lock_between_calls
    daemon = lease_daemon(acquired_status(path))
    with_pool(daemon) do |pool|
      [nil, "server", nil].each do |durability|
        handle = pool.lease(LEASED_NAME, path: path, durability: durability) do |db|
          daemon.steps << "work"
          db
        end
        assert_equal("db", handle)
      end
    end
    assert_equal(%w[acquire open work status work sync status work close release], daemon.steps)
    assert_path_exists(path)
  end

  def test_failed_block_keeps_the_lease_and_does_not_offload
    daemon = lease_daemon(acquired_status(path))
    with_pool(daemon) do |pool|
      assert_raises(RuntimeError) { pool.lease(LEASED_NAME, path: path, offload: true) { raise "work failed" } }
      assert(entries(pool)[LEASED_NAME].leased)
    end
    assert_equal(%w[acquire open close release], daemon.steps)
    assert_equal([{ "name" => LEASED_NAME, "durability" => "server", "timeout_millis" => 600_000 }],
                 daemon.request_bodies("/v1/databases/release"))
  end

  # break, next, and a return out of the block leave work unfinished: they
  # never offload, and never wait for durability.
  def test_a_block_left_early_never_offloads
    daemon = lease_daemon(acquired_status(path))
    with_pool(daemon) do |pool|
      broken = pool.lease(LEASED_NAME, path: path, offload: true, durability: "server") { break :left }
      assert_equal(:left, broken)
      pool.release(LEASED_NAME)
      returned_early = lambda do
        pool.lease(LEASED_NAME, path: path, offload: true, durability: "server") { return :returned }
      end
      assert_equal(:returned, returned_early.call)
      pool.release(LEASED_NAME)
      # next ends the block normally, with its value, so it counts as finished.
      assert_equal(:next, pool.lease(LEASED_NAME, path: path, offload: true) { next :next })
      pool.release(LEASED_NAME)
    end
    offloads = daemon.request_bodies("/v1/databases/release").map { |body| body.fetch("offload", false) }
    assert_equal([false, false, true], offloads)
    refute_includes(daemon.steps, "sync")
  end

  def test_release_ends_the_lease_and_refuses_one_the_pool_does_not_lease
    daemon = lease_daemon(acquired_status(path))
    pool = nil
    with_pool(daemon) do |lease_pool|
      pool = lease_pool
      pool.lease(LEASED_NAME, path: path, durability: "cloud", offload: true) { nil }
      pool.release(LEASED_NAME)
      assert_equal(%w[acquire open sync close release], daemon.steps)
      assert(daemon.request_bodies("/v1/databases/release").first["offload"])
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) { pool.release(LEASED_NAME) }.code)
    end
    assert_equal("closed", assert_raises(Litebackup::Error) { pool.lease(LEASED_NAME) { nil } }.code)
  end

  def test_idle_release_reports_a_failed_release
    Litebackup::Pool.check_seconds = 0.01
    daemon = lease_daemon(acquired_status(path))
    daemon.failing << "release"
    reported = Queue.new
    Litebackup::Client.open(socket_path: daemon.daemon.socket_path) do |client|
      pool = client.databases.pool(PoolStepAdapter.new(daemon.steps), idle_release: 0.05,
                                                                      on_release_error: ->(error) { reported << error })
      pool.lease(LEASED_NAME, path: path) { nil }
      error = reported.pop(timeout: 10)
      assert_equal(["release", LEASED_NAME], [error.step, error.name])
      assert(entries(pool)[LEASED_NAME].leased)
      daemon.failing.clear
      pool.close
    end
    assert_equal("release", daemon.steps.last)
    assert_equal(1, daemon.steps.count("close"))
  end

  # The release the pool's close sends gets what closing left of the cleanup timeout.
  def test_lease_cleanup_timeout_is_shared
    daemon = lease_daemon(acquired_status(path))
    with_pool(daemon, PoolStepAdapter.new(daemon.steps, close_seconds: 0.5), cleanup_timeout: 2) do |pool|
      pool.lease(LEASED_NAME, path: path) { nil }
    end
    release = daemon.request_bodies("/v1/databases/release").first
    assert_includes(1000..1600, release["timeout_millis"])
  end

  # An offloaded rule database used from several threads is brought back
  # once and opened once; lease is refused for it, and use for an acquired database.
  def test_use_brings_back_and_keeps_open
    rule_path = File.join(@working_directory, "42.sqlite")
    daemon = lease_daemon(rule_status(rule_path, "offloaded"))
    with_pool(daemon) do |pool|
      handles = Array.new(4) { Thread.new { pool.use(RULE_NAME) { |db| db } } }.map(&:value)
      assert_equal(["db"] * 4, handles)
      assert_equal([1, 1], [daemon.steps.count("open"), daemon.steps.count("acquire")])
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) { pool.lease(RULE_NAME) { nil } }.code)
      daemon.lock.synchronize do
        daemon.status.merge!("name" => LEASED_NAME, "acquired" => true)
        daemon.status.delete("backup_rule_id")
      end
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) { pool.use(LEASED_NAME) { nil } }.code)
    end
  end

  # A rule database without a local limit stays open while idle; idle_close,
  # or a rule that offloads, closes it.
  [[0, nil, false], [0, 0.05, true], [5, nil, true]].each do |maximum_local_databases, idle_close, closed|
    define_method("test_use_closes_idle_only_when_rule_offloads_#{maximum_local_databases}_#{idle_close.inspect}") do
      Litebackup::Pool.check_seconds = 0.01
      Litebackup::Pool.offload_idle_close_seconds = 0.05
      rule_path = File.join(@working_directory, "42.sqlite")
      FileUtils.touch(rule_path)
      rule = journal_rule(rule_path, false).merge("maximum_local_databases" => maximum_local_databases)
      daemon = lease_daemon(rule_status(rule_path, "synced"), [rule])
      with_pool(daemon, idle_close: idle_close) do |pool|
        pool.use(RULE_NAME) { nil }
        sleep(0.3)
        assert_equal(closed, daemon.steps.include?("close"), daemon.steps.inspect)
      end
    end
  end

  def test_pool_refuses_bad_settings
    Litebackup::Client.open(socket_path: "/nonexistent/client.sock") do |client|
      adapter = PoolStepAdapter.new([])
      [{ cleanup_timeout: 0 }, { cleanup_timeout: -1 }, { idle_release: -1 }].each do |options|
        assert_equal("invalid_argument", assert_raises(Litebackup::Error) do
          client.databases.pool(adapter, **options)
        end.code)
      end
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) { client.databases.pool(nil) }.code)
      client.databases.pool(adapter) do |pool|
        error = assert_raises(Litebackup::Error) { pool.lease("a.sqlite", durability: "bucket") { nil } }
        assert_equal("invalid_argument", error.code)
      end
    end
  end

  def test_create_empty_file_refuses_a_path_whose_directory_is_a_file
    File.write(File.join(@working_directory, "plain"), "")
    error = assert_raises(Litebackup::Error) { Litebackup::Pool.create_empty_file("plain/7.sqlite") }
    assert_equal("invalid_argument", error.code)
  end

  # A lease on a database this machine no longer has closes the handle,
  # drops the lease, and raises lock_lost without calling the block; the
  # next lease acquires again.
  [{ "acquired" => false }, { "state" => "conflicted" }].each_with_index do |lost_status, index|
    define_method("test_lease_on_a_lost_lock_raises_lock_lost_#{index}") do
      daemon = lease_daemon(acquired_status(path))
      with_pool(daemon) do |pool|
        pool.lease(LEASED_NAME, path: path) { nil }
        daemon.lock.synchronize { daemon.status.merge!(lost_status) }
        entered = false
        error = assert_raises(Litebackup::Error) { pool.lease(LEASED_NAME, path: path) { entered = true } }
        assert_equal("lock_lost", error.code)
        refute(entered)
        refute(entries(pool)[LEASED_NAME]&.leased)
        daemon.lock.synchronize { daemon.status.merge!(acquired_status(path)) }
        pool.lease(LEASED_NAME, path: path) { nil }
        assert_equal(%w[acquire open status close acquire open], daemon.steps)
      end
    end
  end

  def test_idle_release_releases_an_unused_lease_with_its_durability
    Litebackup::Pool.check_seconds = 0.01
    daemon = lease_daemon(acquired_status(path))
    Litebackup::Client.open(socket_path: daemon.daemon.socket_path) do |client|
      client.databases.pool(PoolStepAdapter.new(daemon.steps), idle_release: 0.05) do |pool|
        pool.lease(LEASED_NAME, path: path, durability: "cloud") { nil }
        await_reported(10) { daemon.steps.include?("release") }
      end
      client.databases.pool(PoolStepAdapter.new(daemon.steps), idle_release: 0) do |pool|
        pool.lease(LEASED_NAME, path: path) { nil }
        sleep(0.2)
        assert_equal(1, daemon.steps.count("release"))
      end
    end
    assert_equal(%w[acquire open sync close release], daemon.steps.first(5))
    assert_equal("cloud", daemon.request_bodies("/v1/databases/release").first["durability"])
  end

  # close refuses new calls at once, but closes a handle only after the block using it returned.
  %w[use lease].each do |call|
    define_method("test_close_waits_for_active_blocks_in_#{call}") do
      rule_path = File.join(@working_directory, "42.sqlite")
      FileUtils.touch(rule_path)
      daemon = lease_daemon(call == "use" ? rule_status(rule_path, "synced") : acquired_status(rule_path))
      entered = Gate.new
      allowed = Gate.new
      Litebackup::Client.open(socket_path: daemon.daemon.socket_path) do |client|
        pool = client.databases.pool(PoolStepAdapter.new(daemon.steps), idle_close: 60)
        work = lambda do |_db|
          entered.open!
          allowed.wait(10)
          daemon.steps << "block returned"
        end
        blocking = Thread.new do
          call == "use" ? pool.use(RULE_NAME, &work) : pool.lease(LEASED_NAME, path: rule_path, &work)
        end
        entered.wait(10)
        closing = Thread.new { pool.close }
        await_reported { pool.instance_variable_get(:@closing) }
        assert_equal("closed", assert_raises(Litebackup::Error) { pool.lease("agents/8.sqlite") { nil } }.code)
        sleep(0.1)
        refute_includes(daemon.steps, "close")
        allowed.open!
        blocking.join
        closing.join
      end
      assert_operator(daemon.steps.index("block returned"), :<, daemon.steps.index("close"))
    end
  end

  # A release that failed after closing the handle keeps the lease; the next lease opens the handle again.
  def test_lease_reopens_the_handle_after_a_failed_release
    daemon = lease_daemon(acquired_status(path))
    with_pool(daemon) do |pool|
      pool.lease(LEASED_NAME, path: path) { nil }
      daemon.failing << "release"
      error = assert_raises(Litebackup::LeaseCleanupError) { pool.release(LEASED_NAME) }
      assert_equal("release", error.step)
      assert_equal("unavailable", error.cause.code)
      daemon.failing.clear
      assert_equal("db", pool.lease(LEASED_NAME, path: path) { |db| db })
    end
    assert_equal(%w[acquire open close release status open close release], daemon.steps)
  end

  # A rule database whose idle close failed keeps its entry and handle, so the next check closes it.
  # Saves the warnings the SDK reports instead of printing them.
  class WarningLog
    attr_reader :messages

    def initialize
      @messages = Queue.new
    end

    def warn(message)
      @messages << message
    end
  end

  def test_failed_idle_close_keeps_the_handle
    Litebackup::Pool.check_seconds = 0.01
    warnings = WarningLog.new
    Litebackup.logger = warnings
    rule_path = File.join(@working_directory, "42.sqlite")
    FileUtils.touch(rule_path)
    daemon = lease_daemon(rule_status(rule_path, "synced"))
    with_pool(daemon, PoolStepAdapter.new(daemon.steps, failing_closes: 1), idle_close: 0.05) do |pool|
      pool.use(RULE_NAME) { nil }
      await_reported(10) { daemon.steps.count("close") == 2 }
      await_reported(10) { !entries(pool).key?(RULE_NAME) }
    end
    assert_equal(1, daemon.steps.count("open"))
    assert_includes(warnings.messages.pop(timeout: 1), "closing the idle database #{RULE_NAME} failed")
  ensure
    Litebackup.logger = nil
  end

  # on_release_error runs outside the entry's turn and the idle thread, so it can call release and close.
  def test_on_release_error_calls_the_pool
    Litebackup::Pool.check_seconds = 0.01
    daemon = lease_daemon(acquired_status(path))
    daemon.failing << "release"
    released = Gate.new
    pool = nil
    release_again = lambda do |error|
      daemon.failing.clear
      pool.release(error.name)
      released.open!
    end
    Litebackup::Client.open(socket_path: daemon.daemon.socket_path) do |client|
      pool = client.databases.pool(PoolStepAdapter.new(daemon.steps), idle_release: 0.05,
                                                                      on_release_error: release_again)
      pool.lease(LEASED_NAME, path: path) { nil }
      assert(released.wait(10), "the callback never released the lease")
      pool.close
    end
    assert_equal(%w[release release], daemon.steps.last(2))
  end

  # A rollback-journal database under a rule with offload_rollback_journal
  # closes after each use and opens again on the next; WAL databases and
  # rules without the flag stay open.
  [["rollback", true, true], ["rollback", false, false], ["wal", true, false]].each do |journal_mode, offloads, closed|
    define_method("test_use_closes_a_rollback_database_its_rule_offloads_#{journal_mode}_#{offloads}") do
      rule_path = File.join(@working_directory, "42.sqlite")
      FileUtils.touch(rule_path)
      daemon = lease_daemon(rule_status(rule_path, "synced").merge("journal_mode" => journal_mode),
                            [journal_rule(rule_path, offloads)])
      with_pool(daemon) do |pool|
        2.times do
          pool.use(RULE_NAME) do |db|
            assert_equal("db", db)
            refute_equal("close", daemon.steps.last)
          end
        end
        assert_equal(closed, !entries(pool).key?(RULE_NAME))
        assert_equal(1, daemon.steps.count("rules"))
        expected = closed ? %w[status open rules close status open close] : %w[status open rules]
        assert_equal(expected, daemon.steps)
      end
    end
  end

  # A failed close after the last use goes to on_release_error and keeps the
  # entry; the idle check closes it.
  def test_failed_close_after_use_is_reported_and_retried
    Litebackup::Pool.check_seconds = 0.01
    rule_path = File.join(@working_directory, "42.sqlite")
    FileUtils.touch(rule_path)
    daemon = lease_daemon(rule_status(rule_path, "synced").merge("journal_mode" => "rollback"),
                          [journal_rule(rule_path, true)])
    reported = Queue.new
    Litebackup::Client.open(socket_path: daemon.daemon.socket_path) do |client|
      pool = client.databases.pool(PoolStepAdapter.new(daemon.steps, failing_closes: 1),
                                   on_release_error: ->(error) { reported << error })
      pool.use(RULE_NAME) { nil }
      error = reported.pop(timeout: 10)
      assert_equal("close", error.step)
      assert_equal(false, error.details["acquired"])
      await_reported(10) { daemon.steps.count("close") == 2 }
      await_reported(10) { !entries(pool).key?(RULE_NAME) }
      pool.close
    end
    assert_equal(1, daemon.steps.count("open"))
  end

  def test_a_pool_made_before_a_fork_is_refused_in_the_child
    daemon = lease_daemon(acquired_status(path))
    with_pool(daemon) do |pool|
      reader, writer = IO.pipe
      pid = fork do
        reader.close
        codes = [-> { pool.lease(LEASED_NAME) { nil } }, -> { pool.close }].map do |call|
          call.call
          "none"
        rescue Litebackup::Error => e
          e.code
        end
        writer.write(codes.join(" "))
        writer.close
        exit!(0)
      end
      writer.close
      Process.wait(pid)
      assert_equal("invalid_argument invalid_argument", reader.read)
      pool.lease(LEASED_NAME, path: path) { nil }
    end
    assert_equal(%w[acquire open close release], daemon.steps)
  end

  def test_wire_constants_match_the_generated_models
    assert_includes(Litebackup::Models::DatabaseStatus.constants, :ROLE_ORIGIN)
    assert_equal("origin", Litebackup::Wire::ROLE_ORIGIN)
    assert_equal("offloaded", Litebackup::Wire::STATE_OFFLOADED)
    assert_equal("conflicted", Litebackup::Wire::STATE_CONFLICTED)
    assert_equal(%w[server cloud], Litebackup::Durability::ALL)
    assert_equal(%w[lock detect none], Litebackup::WriteProtection::ALL)
  end

  # A durability wait that failed is an unfinished call: the release keeps the copy.
  def test_a_failed_durability_wait_never_offloads
    daemon = lease_daemon(acquired_status(path))
    daemon.failing << "sync"
    with_pool(daemon) do |pool|
      error = assert_raises(Litebackup::Error) { pool.lease(LEASED_NAME, path: path, offload: true, durability: "server") { nil } }
      assert_equal("unavailable", error.code)
      daemon.failing.clear
    end
    assert_equal([false], daemon.request_bodies("/v1/databases/release").map { |body| body.fetch("offload", false) })
  end

  # Opens a stand-in handle slowly, so an interrupt arrives while it opens.
  class SlowOpenAdapter < PoolStepAdapter
    def initialize(steps, opening)
      super(steps)
      @opening = opening
    end

    def open(path)
      @opening << true
      sleep(0.3)
      super
    end
  end

  # An interrupt while the lease opens takes effect once the handle is the
  # pool's, so closing the pool closes it and releases the lock.
  def test_an_interrupt_while_a_lease_opens_leaves_the_lease_to_the_pool
    daemon = lease_daemon(acquired_status(path))
    opening = Queue.new
    with_pool(daemon, SlowOpenAdapter.new(daemon.steps, opening)) do |pool|
      leasing = Thread.new do
        pool.lease(LEASED_NAME, path: path) { daemon.steps << "work" }
      rescue Timeout::Error
        :interrupted
      end
      opening.pop
      leasing.raise(Timeout::Error, "an interrupt while opening")
      assert_equal(:interrupted, leasing.value)
      assert(entries(pool)[LEASED_NAME].leased)
      assert(entries(pool)[LEASED_NAME].opened)
    end
    assert_equal(%w[acquire open close release], daemon.steps)
  end

  def test_an_infinite_cleanup_timeout_is_refused
    Litebackup::Client.open(socket_path: "/nonexistent/client.sock") do |client|
      error = assert_raises(Litebackup::Error) do
        client.databases.pool(PoolStepAdapter.new([]), cleanup_timeout: Float::INFINITY)
      end
      assert_equal("invalid_argument", error.code)
    end
  end
end
