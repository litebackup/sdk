# frozen_string_literal: true

require "monitor"

module Litebackup
  # A failover followed by a background thread; stop, or the client's close,
  # ends following.
  #
  # Each replica asks for the write lock about once a second, so one takes
  # over once the origin's lease runs out. Callbacks run on the following
  # thread, in event order; the first is on_role_change with the role the
  # start answered, and it may run before failovers.start returns, so use its
  # role argument rather than the Failover.
  #
  # role is "origin", "replica", or "unknown": unknown while the daemon does
  # not answer, once the failover stopped, and from a draining or refused
  # event until the next role change or a role read past it. The SDK sets
  # unknown; the daemon never reports it.
  class Failover
    ORIGIN = Models::DatabaseStatus::ROLE_ORIGIN
    REPLICA = Models::DatabaseStatus::ROLE_REPLICA
    UNKNOWN = "unknown"
    EVENT_PAGE = 1000
    ROLE_CHANGED = Models::EventType::ROLE_CHANGED
    DRAINING = Models::EventType::DRAINING
    DENIED = Models::EventType::DENIED
    ROLE_EVENT_TYPES = [ROLE_CHANGED, DRAINING, DENIED].freeze
    # Error codes the event reading never retries: they end following. An
    # expired cursor is not one: a failover resumes at the log's end.
    FATAL_ERROR_CODES = [
      ErrorCode::SDK_UNSUPPORTED, ErrorCode::CLOSED, ErrorCode::CANCELLED, ErrorCode::INVALID_ARGUMENT
    ].freeze

    class << self
      # How long one failover long-poll waits. It is shorter than
      # confirmation_limit_seconds, so a daemon that answers confirms the
      # role in time.
      attr_accessor :event_wait_seconds
      # The longest a failover keeps reporting a role the daemon has not
      # confirmed. 15 s is the default client heartbeat interval and a
      # quarter of the default 60 s session lease another machine waits out
      # before it takes over. Tests shorten it.
      attr_accessor :confirmation_limit_seconds
    end
    self.event_wait_seconds = 10.0
    self.confirmation_limit_seconds = 15.0

    # What the follower carries from one read to the next: the newest event
    # ID handed to the failover (cursor), the backoff after the next failed
    # read (delay), when the daemon last confirmed the role (confirmed),
    # whether the role is read before the next long-poll (role_read_pending)
    # and the log's end before that (log_end_pending), and the log's end read
    # before the last role read, whose role_changed events are older than the
    # read and skipped (role_read_log_end).
    class FollowProgress
      attr_accessor :cursor, :delay, :confirmed, :role_read_pending, :log_end_pending, :role_read_log_end

      def initialize(cursor)
        @cursor = cursor
        @delay = Wire::FIRST_RETRY_SECONDS
        @confirmed = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        @role_read_pending = false
        @log_end_pending = false
        @role_read_log_end = 0
      end
    end

    # What the daemon answered when the failover started.
    attr_reader :status, :name, :path

    def initialize(client, status, cursor, on_role_change: nil, on_denied: nil, on_error: nil)
      @client = client
      @status = status
      @name = status.name
      @path = status.path
      @role = status.role
      @cursor = cursor
      @event_wait = Failover.event_wait_seconds
      @confirmation_limit = Failover.confirmation_limit_seconds
      @on_role_change = on_role_change
      @on_denied = on_denied
      @on_error = on_error
      @stopped = false
      @owner_pid = Process.pid
      # Locked across every callback, so once stop_following returns no
      # callback runs; reentrant, so a callback may stop the failover.
      @callback_lock = Monitor.new
      @wake = Queue.new
      @thread = nil
    end

    # The role this machine has; unknown in a process forked after the
    # failover started, which has no follower and never learns a change.
    def role
      Process.pid == @owner_pid ? @role : UNKNOWN
    end

    # Whether this machine writes the database now; false while the role is unknown.
    def origin?
      role == ORIGIN
    end

    def stopped?
      @stopped
    end

    # Which machine writes the database, when its lease runs out, and whether it ran out.
    def writer
      ensure_owner
      @client.databases.writer(name)
    end

    # Stops the failover and releases its write lock. The file stays.
    # Following ends first, so no callback runs once the failover is stopped.
    def stop
      ensure_owner
      stop_following
      @client.backups.stop(name)
    end

    # Starts the following thread; failovers.start calls it.
    def start_following
      @thread = Thread.new { follow(@cursor) }
      @thread.name = "litebackup-failover-#{name}"
      @thread.report_on_exception = true
      self
    end

    # Stops following events; once it returns, no callback runs. A callback
    # in progress on the following thread is waited for. The thread itself
    # exits when its current long-poll returns; the client's close waits for it.
    def stop_following
      mark_stopped
      @wake << true
      @client.remove_failover(self)
    end

    def join_follower
      @thread.join unless @thread.nil? || @thread == Thread.current
    end

    private

    def ensure_owner
      return if Process.pid == @owner_pid

      raise Litebackup.invalid_argument(
        "this failover was started in process #{@owner_pid} and is not followed after a fork; " \
        "start it again in this process"
      )
    end

    def monotonic
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end

    def role_event?(event_type)
      ROLE_EVENT_TYPES.include?(event_type)
    end

    # The role a status read reports: unknown while the daemon drains the
    # origin, as after a draining event, and while the failover's file is
    # refused (conflicted), as after a denied event.
    def status_role(database)
      database.draining == true || database.state == Wire::STATE_CONFLICTED ? UNKNOWN : database.role
    end

    # Stops callbacks, waiting for one in progress on another thread to
    # return. Nothing follows the role any more, so it is unknown.
    def mark_stopped
      @callback_lock.synchronize do
        @stopped = true
        @role = UNKNOWN
      end
    end

    # Calls on_role_change with the role the start answered, so an app can
    # route writes from its callback alone.
    def report_start_role
      @callback_lock.synchronize do
        @on_role_change.call(@role) if !@stopped && !@on_role_change.nil?
      end
    end

    def apply_event(event, progress)
      @callback_lock.synchronize do
        return if @stopped || event.name != name

        # An event older than the last role read changes no role, because
        # that read has it; a refusal among them is still reported.
        older_than_role_read = role_event?(event.type) && event.id <= progress.role_read_log_end
        return if older_than_role_read && event.type != DENIED

        case event.type
        when ROLE_CHANGED
          change_role(Wire.supplied?(event.role) ? event.role : event.message)
        when DRAINING
          # The daemon drains: it stops being the origin before it releases
          # the lock, so the app stops writing while no machine is allowed to
          # take over yet.
          change_role(UNKNOWN)
        when DENIED
          # The daemon refused this machine's file and the failover stays
          # conflicted there: this machine neither writes nor follows the database.
          change_role(UNKNOWN) unless older_than_role_read
          # The role callback could have stopped the failover.
          @on_denied.call(event.message) if !@stopped && !@on_denied.nil?
        end
      end
    end

    # Asks for a role read after a long-poll that brought no role change
    # while the role is unknown. A drain that failed says nothing more after
    # its draining event; its status shows the role once it stopped.
    def read_unknown_role_again(listing, progress)
      return unless @role == UNKNOWN
      return if listing.events.any? do |event|
        event.name == name && role_event?(event.type) && event.id > progress.role_read_log_end
      end

      progress.role_read_pending = true
      progress.log_end_pending = true
    end

    # Saves a role and calls on_role_change when it differs; returns whether it changed.
    def change_role(new_role)
      @callback_lock.synchronize do
        return false if @stopped || !Wire.supplied?(new_role) || new_role == @role

        @role = new_role
        @on_role_change&.call(new_role)
        true
      end
    end

    def report_error(error)
      @callback_lock.synchronize do
        @on_error.call(error) if !@on_error.nil? && !@stopped
      end
    end

    # Moves role_read_log_end through a page read after it, up to log_end,
    # and returns whether the page changed this database's role.
    def scan_role_change_page(listing, log_end, progress)
      superseded = false
      listing.events.each do |event|
        break if event.id > log_end

        superseded = true if event.name == name && role_event?(event.type)
      end
      unless listing.events.empty?
        # Never past log_end: a role change after it came after the role
        # read, and the long-poll applies it.
        progress.role_read_log_end = [progress.role_read_log_end, [listing.events.last.id, log_end].min].max
      end
      superseded
    end

    # Makes the role unknown without calling on_role_change.
    def clear_role
      @callback_lock.synchronize { @role = UNKNOWN }
    end

    # Makes the role unknown and, when that changed it, has the follower read
    # the log's end and the role once the daemon answers.
    def lose_role(progress)
      return unless change_role(UNKNOWN)

      progress.role_read_pending = true
      progress.log_end_pending = true
    end

    # Makes the role unknown once the daemon has not confirmed it for the confirmation limit.
    def lose_unconfirmed_role(progress)
      lose_role(progress) if @role != UNKNOWN && monotonic - progress.confirmed >= @confirmation_limit
    end

    # The call with a deadline of the confirmation limit after the last
    # confirmation, or after now once the role is unknown.
    def with_answer_deadline(call, progress)
      now = monotonic
      start = @role == UNKNOWN ? now : progress.confirmed
      call.with(answer_deadline_seconds: [0.0, start + @confirmation_limit - now].max)
    end

    # After a failed read: makes the role unknown when the daemon did not
    # answer or following ends, reports the error, and returns how long to
    # wait before the next read, or nil when following ends. While the role
    # is known, the wait ends when the confirmation limit passes.
    def retry_delay(error, progress)
      return nil if @stopped

      ending = FATAL_ERROR_CODES.include?(error.code)
      if error.code == ErrorCode::CLOSED
        # The client is closing and stops following next: no role callback.
        clear_role
      elsif ending || error.status.zero?
        lose_role(progress)
      end
      report_error(error)
      return nil if ending || @stopped

      wait = progress.delay
      wait = [0.0, [wait, progress.confirmed + @confirmation_limit - monotonic].min].max if @role != UNKNOWN
      progress.delay = [progress.delay * 2, Wire::MAXIMUM_RETRY_SECONDS].min
      wait
    end

    # Long-polls the daemon's events after cursor until stopped, passing this
    # failover's events to it. It first reports the role the start answered.
    #
    # An expired cursor does not end following: it moves to the log's end,
    # then the role is read again before the next long-poll, so a role change
    # in events the daemon pruned is still reported. The cursor is read first
    # and kept while the role read is retried, so no event after it is lost.
    #
    # A read the daemon did not answer (connection_failed, timeout,
    # daemon_unsupported: any error with status 0) makes the role unknown at
    # once, and so does no answer within the confirmation limit of the last
    # confirmation, or failed reads the daemon answered for that long. Once
    # the daemon answers again, the follower reads the log's end, then the
    # role, then the log's end again and the events up to it: when one
    # changed the role, the role read could predate it, so it reads the role
    # again. A closed client makes the role unknown without a callback.
    def follow(cursor)
      report_start_role
      progress = FollowProgress.new(cursor)
      until @stopped
        begin
          read_daemon(progress)
        rescue Error => e
          wait = retry_delay(e, progress)
          return if wait.nil?

          @wake.pop(timeout: wait)
          next
        end
        progress.delay = Wire::FIRST_RETRY_SECONDS
      end
    end

    # The follower's next read: the log's end or the role when a role read is
    # pending, one long-poll otherwise.
    def read_daemon(progress)
      lose_unconfirmed_role(progress)
      if progress.role_read_pending
        read_role(progress)
        return
      end
      long_poll = Wire.events_page(after: progress.cursor, wait: @event_wait, limit: EVENT_PAGE)
      begin
        listing = @client.call(with_answer_deadline(long_poll, progress))
      rescue Error => e
        raise if @stopped || e.code != ErrorCode::CURSOR_EXPIRED

        report_error(e)
        return if @stopped

        log_end = @client.call(with_answer_deadline(Wire.events_page(after: -1), progress)).next
        progress.cursor = log_end
        progress.role_read_log_end = log_end
        progress.role_read_pending = true
        progress.log_end_pending = false
        return
      end
      progress.confirmed = monotonic
      listing.events.each do |event|
        progress.cursor = [progress.cursor, event.id].max
        apply_event(event, progress)
      end
      progress.cursor = [progress.cursor, listing.next].max
      read_unknown_role_again(listing, progress)
    end

    def read_role(progress)
      if progress.log_end_pending
        progress.role_read_log_end = @client.call(with_answer_deadline(Wire.events_page(after: -1), progress)).next
        progress.log_end_pending = false
      end
      read = status_role(@client.call(with_answer_deadline(Wire.database(name), progress)))
      # The role read could predate a role change the log now has: read the
      # role again, then look past the new log end.
      return if role_superseded?(progress)

      progress.confirmed = monotonic
      progress.role_read_pending = false
      change_role(read)
    end

    # Reads the log's end as of now, then the events from role_read_log_end
    # through it, without waiting. Returns whether one changed this
    # database's role, so a role read that raced it is not trusted.
    # role_read_log_end moves to that end; events after it came after the
    # role read. When pruning passed role_read_log_end, it returns true with
    # log_end_pending set, so the follower starts again from a fresh log end.
    def role_superseded?(progress)
      log_end = @client.call(with_answer_deadline(Wire.events_page(after: -1), progress)).next
      superseded = false
      while progress.role_read_log_end < log_end
        scan = Wire.events_page(after: progress.role_read_log_end, limit: EVENT_PAGE)
        begin
          listing = @client.call(with_answer_deadline(scan, progress))
        rescue Error => e
          raise unless e.code == ErrorCode::CURSOR_EXPIRED

          progress.log_end_pending = true
          return true
        end
        superseded = scan_role_change_page(listing, log_end, progress) || superseded
        break if listing.events.empty?
      end
      progress.role_read_log_end = [progress.role_read_log_end, log_end].max
      superseded
    end
  end
end
