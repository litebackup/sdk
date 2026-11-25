# frozen_string_literal: true

module Litebackup
  # Opens and closes the handle a Pool yields to its calls. An adapter is any
  # object with these two methods; including this module documents it.
  #
  # open must open an existing file and never create one: a missing file is
  # an offloaded copy the pool brings back, never an empty new database;
  # raise Error with code not_found for it. close must roll back a
  # transaction a call left unfinished, never commit it, and raise when a
  # connection may still be open: the pool releases a leased database only
  # after close succeeds. Litebackup::Integrations::SQLite3.adapter is one.
  module Adapter
    def open(path)
      raise NotImplementedError, "#{self.class} must open #{path}"
    end

    def close(handle)
      raise NotImplementedError, "#{self.class} must close #{handle.inspect}"
    end
  end

  # Seconds that limit closing and releasing a leased database together
  # unless cleanup_timeout says otherwise.
  DEFAULT_CLEANUP_TIMEOUT = 600.0

  # The warnings the pool and the integrations report: to logger when one is
  # set, such as Rails.logger, and to standard error otherwise.
  class << self
    attr_accessor :logger

    def report_warning(message)
      if logger.nil?
        warn("litebackup: #{message}")
      else
        logger.warn("litebackup: #{message}")
      end
    end
  end

  # Keeps databases open for calls; safe to share between threads. Create it
  # with databases.pool:
  #
  #   litebackup.databases.pool(Litebackup::Integrations::SQLite3.adapter) do |pool|
  #     pool.lease("agents/42.sqlite", path: "data/agents/42.sqlite") do |db|
  #       db.execute("INSERT INTO steps (body) VALUES (?)", ["tests passed"])
  #     end
  #   end
  #
  # use is for a database a backup rule on this machine covers: it stays here,
  # open between calls. lease is for a database that moves between machines:
  # the first lease acquires the write lock, and the pool keeps it, with the
  # open handle, until idle_release runs out (a minute by default), release,
  # or close. The app coordinates handovers: another machine's lease with
  # lock_timeout succeeds once this one released the database. A lease on a
  # database whose lock was lost meanwhile fails with lock_lost before its
  # block, and the next lease acquires again. A close that fails never
  # releases: a connection may still be open, and another machine must not
  # write while this one still can.
  #
  # Close the pool when done: it waits for active blocks, then releases every
  # leased database and closes every open one. Never close a pool from inside
  # one of its blocks. A pool made before a fork is refused in the child.
  class Pool
    DEFAULT_IDLE_RELEASE = 60.0
    # Seconds a database whose backup rule offloads (maximum_local_databases)
    # stays open unused before the pool closes it, so the rule offloads it,
    # unless idle_close says otherwise. It is well inside the client's
    # 10-minute idle window.
    DEFAULT_OFFLOAD_IDLE_CLOSE = 120.0
    # How often a lease with cloud durability asks whether the bucket has its version yet.
    CLOUD_DURABILITY_POLL_SECONDS = 1.0
    # A release timeout of zero means the daemon's default, so a spent
    # cleanup timeout sends the smallest one instead.
    MINIMUM_RELEASE_SECONDS = 0.001
    IDLE_RELEASE_STEP = "release"
    IDLE_CLOSE_STEP = "close"

    class << self
      # How often a pool looks for idle databases, and how long a database
      # whose rule offloads stays open unused; tests shorten them.
      attr_accessor :check_seconds, :offload_idle_close_seconds
    end
    self.check_seconds = 15.0
    self.offload_idle_close_seconds = DEFAULT_OFFLOAD_IDLE_CLOSE

    # What a pool was created with, checked once. idle_release nil never
    # releases an unused lease.
    Settings = Data.define(:idle_close, :idle_release, :cleanup_seconds, :machine_label, :on_release_error) do
      def self.build(adapter, idle_close:, idle_release:, cleanup_timeout:, machine_label:, on_release_error:)
        raise Litebackup.invalid_argument("a pool needs an adapter") if adapter.nil?

        cleanup_seconds = if cleanup_timeout.nil?
                            DEFAULT_CLEANUP_TIMEOUT
                          else
                            Wire.seconds(cleanup_timeout, "the cleanup timeout")
                          end
        raise Litebackup.invalid_argument("the cleanup timeout must be positive") if cleanup_seconds.nil?

        new(
          idle_close: Wire.seconds(idle_close, "the idle close"),
          idle_release: idle_release.nil? ? DEFAULT_IDLE_RELEASE : Wire.seconds(idle_release, "the idle release"),
          cleanup_seconds: cleanup_seconds,
          machine_label: machine_label,
          on_release_error: on_release_error
        )
      end
    end

    # One lease call's settings; the release that ends the lease uses the last call's.
    LeaseSettings = Data.define(:path, :require_existing, :durability, :offload, :lock_timeout, :acquire_timeout,
                                :maintenance) do
      def self.build(path:, require_existing:, durability:, offload:, lock_timeout:, acquire_timeout:, maintenance:)
        unless durability.nil? || Durability::ALL.include?(durability)
          raise Litebackup.invalid_argument("a lease's durability must be server or cloud")
        end

        new(path: path, require_existing: require_existing, durability: durability, offload: offload,
            lock_timeout: Wire.seconds(lock_timeout, "the lock timeout"),
            acquire_timeout: Wire.seconds(acquire_timeout, "the acquire timeout"),
            maintenance: maintenance)
      end

      def release_durability
        durability.nil? ? Durability::SERVER : durability
      end
    end

    # One database of a pool. turn serializes opening, bringing back,
    # acquiring, lease calls, and releasing; use calls share the open handle
    # without it. users and last_used are guarded by the pool's mutex.
    class Entry
      attr_reader :name, :turn
      # handle is open while opened is set; leased is set while this pool
      # has the write lock through lease; last_call_failed keeps the release
      # from offloading after a failed call; idle_close is the seconds a
      # rule's database stays open unused, nil never closing it;
      # close_after_use is set for a rollback-journal database whose rule
      # offloads rollback-journal databases, which the pool closes as soon as
      # the last use block exits; users counts calls between joining and
      # leaving, blocks included.
      attr_accessor :handle, :path, :opened, :leased, :lease_settings, :last_call_failed, :idle_close,
                    :close_after_use, :users, :last_used

      def initialize(name)
        @name = name
        @turn = Mutex.new
        @handle = nil
        @path = ""
        @opened = false
        @leased = false
        @lease_settings = nil
        @last_call_failed = false
        @idle_close = nil
        @close_after_use = false
        @users = 0
        @last_used = nil
      end
    end

    def initialize(client, adapter, settings)
      @client = client
      @adapter = adapter
      @settings = settings
      @owner_pid = Process.pid
      @mutex = Mutex.new
      @calls_done = ConditionVariable.new
      @stop_signal = ConditionVariable.new
      @entries = {}
      @rules = {}
      @active_calls = 0
      @closing = false
      @stopped = false
      @idle_thread = Thread.new { close_idle_databases }
      @idle_thread.name = "litebackup-pool-idle"
    end

    # Yields the open handle of a database a backup rule on this machine
    # covers, and returns what the block returns.
    #
    # The pool opens it on first use and keeps it open; an offloaded copy is
    # brought back first with acquire, never recreated empty. A
    # rollback-journal database under a rule with offload_rollback_journal is
    # closed as soon as its last use block exits instead, because that rule
    # offloads it while nothing has it open. Uses of one database share the
    # handle, concurrently, so the adapter's handle must be safe to share
    # between threads. A name no rule on this machine covers, or an acquired
    # database, is refused with invalid_argument; lease it instead.
    def use(name, &block)
      raise Litebackup.invalid_argument("use needs a block") if block.nil?

      Thread.handle_interrupt(Object => :never) do
        entry = join(name)
        begin
          Thread.handle_interrupt(Object => :immediate) do
            handle = entry.turn.synchronize do
              raise Litebackup.invalid_argument("#{name} is leased in this pool; use lease for it") if entry.leased

              # Opening is never interrupted halfway, so an open handle is always the entry's.
              Thread.handle_interrupt(Object => :never) { open_rule_database(entry) } unless entry.opened
              entry.handle
            end
            block.call(handle)
          end
        ensure
          leave(entry)
          close_after_last_use(entry) if entry.close_after_use
        end
      end
    end

    # Yields the open handle of a database that moves between machines,
    # exclusively, and returns what the block returns.
    #
    # Leases of one name run one at a time, and no other machine writes while
    # the pool keeps the lock. The first lease acquires the write lock,
    # creating an empty file and its directory at path when nothing is there
    # unless require_existing, and opens it; later leases reuse the lock and
    # the handle. lock_timeout is how long that acquire waits while another
    # machine has the write lock; without it the lease fails at once with
    # lock_unavailable. acquire_timeout limits downloading the head;
    # maintenance checkpoints on release and VACUUMs past the freelist
    # threshold.
    #
    # A later lease first asks the daemon whether this machine still has the
    # lock: when it lost it, the handle is closed, the lease dropped, and
    # lock_lost raised without calling the block; the next lease acquires
    # again. A loss while a block is active shows at the next lease.
    #
    # With durability, a block that ends normally syncs and returns only once
    # the server, or the bucket with "cloud", has what it committed; without
    # it, the block's writes sync in the background. The release that ends
    # the lease waits for the same durability, the server when nil, and with
    # offload removes the local file, unless the last call did not finish:
    # a block that raised or that the code left with break or return, or a
    # durability wait that failed, keeps the lease and its local copy, and
    # never offloads. next ends the block normally.
    #
    # Checking, acquiring, and opening the lease, and the release that ends
    # it, are never interrupted halfway: Timeout, Thread#raise, and Ctrl-C
    # take effect once they return, so an acquired lock and an open handle
    # always stay the pool's to release.
    #
    # The lease ends after idle_release without a block, on release, or on
    # close. The adapters never commit for the block: commit what it should keep.
    def lease(name, path: nil, require_existing: false, durability: nil, offload: false, lock_timeout: nil,
              acquire_timeout: nil, maintenance: false, &block)
      raise Litebackup.invalid_argument("lease needs a block") if block.nil?

      lease_settings = LeaseSettings.build(path: path, require_existing: require_existing, durability: durability,
                                           offload: offload, lock_timeout: lock_timeout,
                                           acquire_timeout: acquire_timeout, maintenance: maintenance)
      Thread.handle_interrupt(Object => :never) do
        entry = join(name)
        begin
          Thread.handle_interrupt(Object => :immediate) do
            entry.turn.synchronize do
              if entry.opened && !entry.leased
                raise Litebackup.invalid_argument("#{name} is a backup rule's database in this pool; use use for it")
              end

              Thread.handle_interrupt(Object => :never) { prepare_lease(entry, lease_settings) }
              finished = false
              begin
                block_value = block.call(entry.handle)
                wait_durable(name, lease_settings.durability) unless lease_settings.durability.nil?
                finished = true
              ensure
                entry.last_call_failed = !finished
              end
              block_value
            end
          end
        ensure
          leave(entry)
        end
      end
    end

    # Ends the lease of a database once an active lease block returns: closes
    # the handle, then releases the write lock once the final version reaches
    # the lease's durability. Raises LeaseCleanupError when closing or
    # releasing fails, and invalid_argument for a database the pool does not lease.
    def release(name)
      ensure_owner
      entry = @mutex.synchronize { @entries[name] }
      raise Litebackup.invalid_argument("#{name} is not leased in this pool") if entry.nil?

      entry.turn.synchronize do
        raise Litebackup.invalid_argument("#{name} is not leased in this pool") unless entry.leased

        release_lease(entry)
      end
      nil
    end

    # Refuses new calls, waits for every active use and lease block, then
    # releases every leased database and closes every open one. A failure
    # raises alone, several as an ErrorGroup; a LeaseCleanupError names a
    # database that stays acquired. A second close returns at once.
    def close
      ensure_owner
      return nil unless start_closing

      @mutex.synchronize do
        @calls_done.wait(@mutex) until @active_calls.zero?
        @stopped = true
        @stop_signal.broadcast
      end
      @idle_thread.join unless @idle_thread == Thread.current
      failures = []
      all_entries.each do |entry|
        entry.turn.synchronize do
          if entry.leased
            begin
              release_lease(entry)
            rescue LeaseCleanupError => e
              failures << e
            end
          elsif entry.opened
            failure = Thread.handle_interrupt(Object => :never) { close_rule_database(entry) }
            failures << failure unless failure.nil?
          end
        end
      end
      Litebackup.raise_together(failures, "closing the pool")
      nil
    end

    private

    # Closes an open rule database and drops its entry; returns the close's
    # error, or nil. The caller has entry's turn.
    def close_rule_database(entry)
      @adapter.close(entry.handle)
      entry.handle = nil
      entry.opened = false
      remove_unused_entry(entry)
      nil
    rescue StandardError => e
      e
    end

    # Checks a kept lease, or acquires a new one, and opens its handle; the
    # caller has entry's turn.
    def prepare_lease(entry, lease_settings)
      check_lease(entry) if entry.leased
      entry.lease_settings = lease_settings
      if !entry.leased
        acquire_lease(entry, lease_settings)
      elsif !entry.opened
        reopen_lease(entry)
      end
    end

    def ensure_owner
      return if Process.pid == @owner_pid

      raise Litebackup.invalid_argument(
        "this pool was made in process #{@owner_pid} and cannot be used after a fork; make a new pool in this process"
      )
    end

    def pool_closed
      Error.new(ErrorCode::CLOSED, "the pool is closed")
    end

    def monotonic
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end

    # Counts a call on the name's entry, creating it.
    def join(name)
      ensure_owner
      raise Litebackup.invalid_argument("a pool call needs a database name") unless Wire.supplied?(name)

      @mutex.synchronize do
        raise pool_closed if @closing

        entry = (@entries[name] ||= Entry.new(name))
        entry.users += 1
        @active_calls += 1
        entry
      end
    end

    # Ends a call on an entry; an entry with nothing open or leased is dropped.
    def leave(entry)
      @mutex.synchronize do
        entry.users -= 1
        @active_calls -= 1
        entry.last_used = monotonic
        if entry.users.zero? && !entry.opened && !entry.leased && @entries[entry.name].equal?(entry)
          @entries.delete(entry.name)
        end
        @calls_done.broadcast if @closing && @active_calls.zero?
      end
    end

    # Refuses new calls; false when the pool was closing already.
    def start_closing
      @mutex.synchronize do
        return false if @closing

        @closing = true
        true
      end
    end

    def all_entries
      @mutex.synchronize { @entries.values }
    end

    def idle_entries
      @mutex.synchronize { @entries.values.select { |entry| entry.users.zero? && !entry.last_used.nil? } }
    end

    # How long an entry has been unused, or nil while a call uses it.
    def idle_seconds(entry, now)
      @mutex.synchronize do
        entry.users.positive? || entry.last_used.nil? ? nil : now - entry.last_used
      end
    end

    # Drops an entry with nothing open or leased once no call uses it.
    def remove_unused_entry(entry)
      @mutex.synchronize do
        if @entries[entry.name].equal?(entry) && entry.users.zero? && !entry.opened && !entry.leased
          @entries.delete(entry.name)
        end
      end
    end

    def rule_lookup_needed?(status)
      @settings.idle_close.nil? || status.journal_mode == Wire::JOURNAL_MODE_ROLLBACK
    end

    # Sets when a rule's database closes: after its idle close, and after
    # each use under a rule that offloads rollback-journal databases.
    def set_rule_closing(entry, status, rule)
      entry.idle_close = idle_close_seconds(rule)
      entry.close_after_use = status.journal_mode == Wire::JOURNAL_MODE_ROLLBACK && !rule.nil? &&
                              rule.offload_rollback_journal == true
    end

    # Whether an entry that closes after use has no active call and an open
    # handle; the caller has its turn.
    def last_use_ended?(entry)
      @mutex.synchronize { entry.close_after_use && entry.users.zero? && entry.opened && !entry.leased }
    end

    def idle_close_seconds(rule)
      return @settings.idle_close unless @settings.idle_close.nil?
      return Pool.offload_idle_close_seconds if !rule.nil? && rule.maximum_local_databases.to_i.positive?

      nil
    end

    def release_durability_and_offload(entry)
      lease_settings = entry.lease_settings
      return [Durability::SERVER, false] if lease_settings.nil?

      [lease_settings.release_durability, lease_settings.offload == true && !entry.last_call_failed]
    end

    # What an idle check does with an entry whose turn the caller has: release, close, or nothing.
    def idle_step(entry, now)
      idle = idle_seconds(entry, now)
      return nil if idle.nil?

      idle_release = @settings.idle_release
      return IDLE_RELEASE_STEP if entry.leased && !idle_release.nil? && idle >= idle_release
      # Its close after the last use failed: try again at once.
      return IDLE_CLOSE_STEP if !entry.leased && entry.opened && entry.close_after_use
      return IDLE_CLOSE_STEP if !entry.leased && entry.opened && !entry.idle_close.nil? && idle >= entry.idle_close

      nil
    end

    def acquired_path(acquired, lease_settings, name)
      unless acquired.database.acquired
        raise Litebackup.invalid_argument("#{name} is a backup rule's database on this machine; use use for it")
      end
      return acquired.database.path unless acquired.database.path.to_s.empty?

      Wire.supplied?(lease_settings.path) ? Wire.absolute_path(lease_settings.path) : ""
    end

    def rule_database?(status)
      status.role == Wire::ROLE_ORIGIN && !status.acquired && !status.failover && Wire.supplied?(status.backup_rule_id)
    end

    # Closes a database its rule offloads while nothing has it open, once no use block is active.
    def close_after_last_use(entry)
      failure = nil
      entry.turn.synchronize do
        return unless last_use_ended?(entry)

        begin
          @adapter.close(entry.handle)
        rescue StandardError => e
          failure = use_close_failed(entry, e)
        else
          entry.handle = nil
          entry.opened = false
          remove_unused_entry(entry)
        end
      end
      report_release_error(failure) unless failure.nil?
    end

    # Opens a rule's database, bringing an offloaded copy back first, and
    # tries once more when the copy was offloaded between the two.
    def open_rule_database(entry)
      status = nil
      handle = nil
      2.times do |attempt|
        status = @client.databases.get(entry.name)
        unless rule_database?(status)
          raise Litebackup.invalid_argument(
            "#{entry.name} is not a backup rule's database on this machine; " \
            "use lease for a database that moves between machines"
          )
        end
        @client.databases.acquire(entry.name) if status.state == Wire::STATE_OFFLOADED
        begin
          handle = @adapter.open(status.path)
        rescue StandardError
          next if attempt.zero? && !File.exist?(status.path)

          raise
        end
        break
      end
      entry.handle = handle
      entry.path = status.path
      entry.opened = true
      rule = rule_lookup_needed?(status) ? database_rule(status.backup_rule_id) : nil
      set_rule_closing(entry, status, rule)
    end

    # The backup rule with rule_id, from the pool's cache or rules.list.
    def database_rule(rule_id)
      rule = @mutex.synchronize { @rules[rule_id] }
      return rule unless rule.nil?

      listed = @client.rules.list
      @mutex.synchronize do
        listed.each { |listed_rule| @rules[listed_rule.id] = listed_rule }
        @rules[rule_id]
      end
    end

    # Drops a lease whose lock this machine lost and raises lock_lost; the caller has entry's turn.
    def check_lease(entry)
      status = begin
        @client.databases.get(entry.name)
      rescue Error => e
        raise unless e.code == ErrorCode::NOT_FOUND

        nil
      end
      return unless status.nil? || !status.acquired || status.state == Wire::STATE_CONFLICTED

      drop_lost_lease(entry)
      raise Error.new(
        ErrorCode::LOCK_LOST,
        "this machine no longer has the write lock of #{entry.name}, so the pool dropped its lease; " \
        "the next lease acquires it again"
      )
    end

    # Opens the handle of a lease whose release failed after its handle closed.
    def reopen_lease(entry)
      begin
        entry.handle = @adapter.open(entry.path)
      rescue Exception # rubocop:disable Lint/RescueException
        entry.last_call_failed = true
        raise
      end
      entry.opened = true
    end

    # Acquires the write lock and opens the database; one that does not open is released again at once.
    def acquire_lease(entry, lease_settings)
      if Wire.supplied?(lease_settings.path) && !lease_settings.require_existing
        Pool.create_empty_file(lease_settings.path)
      end
      acquired = @client.databases.acquire(
        entry.name,
        path: lease_settings.path, maintenance: lease_settings.maintenance,
        machine_label: @settings.machine_label, timeout: lease_settings.acquire_timeout,
        lock_timeout: lease_settings.lock_timeout
      )
      path = acquired_path(acquired, lease_settings, entry.name)
      begin
        handle = @adapter.open(path)
      rescue StandardError => e
        # Nothing is open: release the lock again, without offload.
        entry.path = path
        entry.leased = true
        entry.last_call_failed = true
        begin
          release_lease(entry)
        rescue LeaseCleanupError => release_error
          Litebackup.raise_together([e, release_error], "leasing #{entry.name}")
        end
        raise
      end
      entry.handle = handle
      entry.path = path
      entry.opened = true
      entry.leased = true
    end

    # Syncs now and returns once the server, or the bucket with cloud, has the version the sync reached.
    def wait_durable(name, durability)
      status = @client.databases.sync(name)
      return unless durability == Durability::CLOUD

      target = status.synced_server.version
      while status.synced_cloud.version < target
        sleep(CLOUD_DURABILITY_POLL_SECONDS)
        status = @client.databases.get(name)
      end
    end

    # Closes the handle and releases the lock within one cleanup timeout; the
    # caller has entry's turn. Closing is never cut short; the release gets
    # what closing left. Neither is interrupted halfway, so the entry always
    # says whether the handle is open and the lock is the pool's.
    def release_lease(entry)
      Thread.handle_interrupt(Object => :never) { close_and_release(entry) }
    end

    def close_and_release(entry)
      deadline = monotonic + @settings.cleanup_seconds
      if entry.opened
        begin
          @adapter.close(entry.handle)
        rescue StandardError => e
          raise close_failed(entry, e), cause: e
        end
        entry.handle = nil
        entry.opened = false
      end
      durability, offload = release_durability_and_offload(entry)
      timeout = [deadline - monotonic, MINIMUM_RELEASE_SECONDS].max
      begin
        @client.databases.release(entry.name, durability: durability, offload: offload, timeout: timeout)
      rescue StandardError => e
        raise release_failed(entry, durability, e), cause: e
      end
      entry.leased = false
      remove_unused_entry(entry)
    end

    # Closes the handle of a database whose lock was lost, quietly, and drops
    # the lease; the copy stays as the daemon left it. The caller has entry's turn.
    def drop_lost_lease(entry)
      if entry.opened
        begin
          @adapter.close(entry.handle)
        rescue StandardError => e
          Litebackup.report_warning("closing #{entry.name} after its write lock was lost failed: #{e.message}")
        end
        entry.handle = nil
        entry.opened = false
      end
      entry.leased = false
    end

    def close_failed(entry, error)
      LeaseCleanupError.new(
        LeaseCleanupStep::CLOSE, entry.name,
        "closing #{entry.name} failed, so it was not released and stays acquired by this machine: #{error.message}. " \
        "Close every connection to #{entry.path}, then call pool.release(#{entry.name.inspect}) again",
        { "step" => LeaseCleanupStep::CLOSE, "name" => entry.name, "path" => entry.path, "acquired" => true }
      )
    end

    def release_failed(entry, durability, error)
      LeaseCleanupError.new(
        LeaseCleanupStep::RELEASE, entry.name,
        "releasing #{entry.name} failed: #{error.message}. Its file at #{entry.path} is intact and this machine " \
        "might still have its write lock; call pool.release(#{entry.name.inspect}) again",
        { "step" => LeaseCleanupStep::RELEASE, "name" => entry.name, "path" => entry.path, "durability" => durability }
      )
    end

    def use_close_failed(entry, error)
      failure = LeaseCleanupError.new(
        LeaseCleanupStep::CLOSE, entry.name,
        "closing #{entry.name} after its last use failed, so it stays open and its rule does not offload it: " \
        "#{error.message}. The pool tries again at its next idle check",
        { "step" => LeaseCleanupStep::CLOSE, "name" => entry.name, "path" => entry.path, "acquired" => false }
      )
      Pool.with_cause(failure, error)
    end

    # Closes rule databases past their idle close and releases leases past
    # idle_release, until the pool closes.
    def close_idle_databases
      loop do
        stopped = @mutex.synchronize do
          @stop_signal.wait(@mutex, Pool.check_seconds) unless @stopped
          @stopped
        end
        break if stopped

        now = monotonic
        idle_entries.each do |entry|
          failure = close_idle_entry(entry, now)
          report_release_error(failure) unless failure.nil?
        end
      end
    end

    # Releases or closes one idle entry; returns a failed release to report once its turn is free.
    def close_idle_entry(entry, now)
      return nil unless entry.turn.try_lock

      begin
        case idle_step(entry, now)
        when IDLE_RELEASE_STEP
          begin
            release_lease(entry)
          rescue LeaseCleanupError => e
            return e
          end
        when IDLE_CLOSE_STEP
          begin
            @adapter.close(entry.handle)
          rescue StandardError => e
            # The entry keeps its handle, so the next check and close try again.
            Litebackup.report_warning("closing the idle database #{entry.name} failed: #{e.message}")
            return nil
          end
          entry.handle = nil
          entry.opened = false
          remove_unused_entry(entry)
        end
        nil
      ensure
        entry.turn.unlock
      end
    end

    # Passes a failed background release to on_release_error on its own
    # thread, which close never waits for, so the callback may call the pool;
    # reports it as a warning without a callback.
    def report_release_error(error)
      callback = @settings.on_release_error
      if callback.nil?
        Litebackup.report_warning("the pool could not release #{error.name}: #{error.message}")
        return
      end
      Thread.new { callback.call(error) }.name = "litebackup-pool-release-error"
    end

    class << self
      # Creates an empty file and its directory unless something is already
      # there. The daemon treats an empty file as no copy: a new database when
      # the server has none, replaced by the server's head otherwise.
      def create_empty_file(path)
        file = Wire.absolute_path(path)
        begin
          # A file where the directory should be fails here too.
          FileUtils.mkdir_p(File.dirname(file))
        rescue SystemCallError => e
          raise Litebackup.invalid_argument("creating #{file} failed: #{e.message}")
        end
        begin
          File.open(file, File::CREAT | File::EXCL | File::WRONLY, 0o644, &:close)
        rescue Errno::EEXIST
          nil
        rescue SystemCallError => e
          raise Litebackup.invalid_argument("creating #{file} failed: #{e.message}")
        end
      end

      # The error with cause set, as raise sets it, for an error that is reported rather than raised.
      def with_cause(error, cause)
        raise error, cause: cause
      rescue LeaseCleanupError => e
        e
      end
    end
  end
end
