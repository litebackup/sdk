# frozen_string_literal: true

require_relative "test_helper"

REFUSED_MESSAGE = "/data/app.sqlite differs from the server head; it was left as it is"
APP_DATABASE = { "name" => "app.sqlite", "path" => "/data/app.sqlite", "role" => "origin", "state" => "syncing" }.freeze

def failover_start(role, this_machine:)
  writer = { "name" => "app.sqlite", "active" => true, "this_machine" => this_machine, "writer_machine_label" => "a" }
  { "name" => "app.sqlite", "path" => "/data/app.sqlite", "role" => role, "database" => APP_DATABASE,
    "writer" => writer }
end

def role_event(id, role, name = "app.sqlite")
  { "id" => id, "time" => "2026-01-01T00:00:00Z", "type" => "role_changed", "message" => role, "name" => name,
    "role" => role }
end

# Answers a failover start, then every long-poll with a role change, so role callbacks keep coming.
class FailoverDaemon
  attr_reader :daemon, :stopped_when_stop_arrived, :starts
  attr_accessor :failover, :writer_gate

  def initialize
    @page = 0
    @stopped_when_stop_arrived = []
    @starts = []
    @failover = nil
    @writer_gate = nil
    @daemon = ContractDaemon.new { |received| reply(received) }
  end

  def reply(received)
    case received.path
    when "/v1/failovers/start"
      @starts << received.json
      json_answer(failover_start("origin", this_machine: true))
    when "/v1/backup-rules/stop"
      @stopped_when_stop_arrived << (!@failover.nil? && @failover.stopped?)
      json_answer("lease_pending" => false)
    when "/v1/databases/writer"
      @writer_gate&.wait(10)
      json_answer("name" => "app.sqlite", "active" => true, "this_machine" => true)
    else
      next_role_page(received)
    end
  end

  def next_role_page(received)
    return json_answer("events" => [], "next" => 0) if received.query["after"] == "-1"

    sleep(0.01)
    @page += 1
    json_answer("events" => [role_event(@page, @page.odd? ? "replica" : "origin")], "next" => @page)
  end
end

# Answers a failover's first long-poll with cursor_expired, then a fresh
# cursor, a role of origin, and one role change.
class ExpiredCursorDaemon
  attr_reader :daemon, :reads

  def initialize(failed_role_reads = 0)
    @failed_role_reads = failed_role_reads
    @reads = []
    @log_end_reads = 0
    @role_reads = 0
    @fresh_pages = 0
    @lock = Mutex.new
    @daemon = ContractDaemon.new { |received| @lock.synchronize { reply(received) } }
  end

  def reply(received)
    after = received.query.fetch("after", "0")
    if received.path == "/v1/failovers/start"
      json_answer(failover_start("replica", this_machine: false))
    elsif received.path == "/v1/backup-rules/stop"
      @reads << "stop"
      json_answer("lease_pending" => false)
    elsif received.path == "/v1/databases/status"
      @role_reads += 1
      if @role_reads <= @failed_role_reads
        @reads << "role failed"
        return json_answer({ "code" => "unavailable", "message" => "the daemon is busy" }, 503)
      end
      @reads << "role"
      json_answer(APP_DATABASE)
    elsif after == "-1"
      @log_end_reads += 1
      @reads << "log end" if @log_end_reads > 1
      json_answer("events" => [], "next" => @log_end_reads == 1 ? 7 : 20)
    elsif after == "7"
      json_answer({ "code" => "cursor_expired", "message" => "events after 7 are no longer kept" }, 410)
    elsif after == "20" && @fresh_pages.zero?
      @fresh_pages += 1
      @reads << "after 20"
      json_answer("events" => [role_event(21, "replica")], "next" => 21)
    else
      sleep(0.05)
      json_answer("events" => [], "next" => after.to_i)
    end
  end

  # The errors and roles the failover reports, then the reads the daemon
  # sees, once it follows from the fresh cursor.
  def resume_expectations
    role_read_failed = @failed_role_reads.positive?
    reported = ["replica", "cursor_expired", *(role_read_failed ? ["unavailable"] : []), "origin", "replica"]
    # The log's end is read once: a failed role read keeps the cursor it read.
    [reported, ["log end", *(role_read_failed ? ["role failed"] : []), "role", "log end", "after 20"]]
  end
end

# Starts an origin failover, then answers long-polls and role reads
# normally, every one with an error, or none. Once returned is set, the
# log's end is 9 and the long-poll from the start's cursor 7 has a role
# change older than the role read and a refusal made while the daemon was stopped.
class RoleDaemon
  attr_reader :daemon, :role_reads, :log_end_cursors, :hang
  attr_accessor :returned, :role_read, :raced_changes, :conflicted_from_read

  def initialize(long_polls = "empty pages")
    @long_polls = long_polls
    @returned = false
    @role_read = "origin"
    @role_reads = 0
    @raced_changes = { "events" => [], "next" => 9 }
    @conflicted_from_read = 0
    @log_end_cursors = []
    @hang = Gate.new
    @daemon = ContractDaemon.new { |received| reply(received) }
  end

  def reply(received)
    after = received.query.fetch("after", "0")
    if received.path == "/v1/failovers/start"
      json_answer(failover_start("origin", this_machine: true))
    elsif received.path == "/v1/backup-rules/stop"
      json_answer("lease_pending" => false)
    elsif !received.query.key?("wait_millis") && received.path == "/v1/events/list"
      log_page(after)
    elsif @long_polls != "empty pages"
      # Long-polls and role reads fail alike, so the role stays unknown.
      @hang.wait(10) if @long_polls == "no answer"
      json_answer({ "code" => "unavailable", "message" => "the daemon is busy" }, 503)
    elsif received.path == "/v1/databases/status"
      @role_reads += 1
      body = APP_DATABASE.merge("role" => @role_read)
      body["state"] = "conflicted" if @conflicted_from_read.positive? && @conflicted_from_read <= @role_reads
      json_answer(body)
    elsif @returned && after == "7"
      older = role_event(8, "replica")
      refusal = { "id" => 9, "time" => "2026-01-01T00:01:00Z", "type" => "refused", "message" => REFUSED_MESSAGE,
                  "name" => "app.sqlite", "path" => "/data/app.sqlite" }
      raced = @raced_changes["events"]
      json_answer("events" => [older, refusal, *raced], "next" => [9, *raced.map { |event| event["id"] }].max)
    else
      sleep(0.05)
      json_answer("events" => [], "next" => after.to_i)
    end
  end

  def log_page(after)
    # The look past the log end after the role read.
    if @returned && after != "-1"
      return json_answer(after == "9" ? @raced_changes : { "events" => [],
                                                           "next" => after.to_i })
    end

    @log_end_cursors << after if @returned
    # Once returned, the first log end read is 9 and later ones find the raced changes' end.
    log_end = if !@returned then 7
              elsif @log_end_cursors.length == 1 then 9
              else @raced_changes["next"]
              end
    json_answer("events" => [], "next" => log_end)
  end

  def close
    @hang.open!
    @daemon.close
  end
end

# A page of events after after, as the daemon pages them: up to 1000,
# through newest, with next the last ID delivered. All are another
# database's, except this database's change to origin at 2500 when
# own_change_in_scan is set, and its change to replica at 2601, which exists
# only after the last look past the role read stopped at 2600.
def fixed_log_end_events(after, own_change_in_scan, newest)
  events = ((after + 1)..[after + 1000, newest].min).map do |event_id|
    event = role_event(event_id, "origin", "second.db")
    event["name"] = "app.sqlite" if event_id == 2500 && own_change_in_scan
    event.merge!("name" => "app.sqlite", "role" => "replica", "message" => "replica") if event_id == 2601
    event
  end
  { "events" => events, "next" => events.empty? ? after : events.last["id"] }
end

# Expires a failover's first cursor, then pages events through a log end of
# 2600 and one change after it.
class FixedLogEndDaemon
  attr_reader :daemon, :role_reads, :want_role_reads

  def initialize(scan_condition)
    @scan_condition = scan_condition
    @log_ends = [7, 100]
    @scans = 0
    @role_reads = 0
    @want_role_reads = scan_condition == "unrelated events" ? 1 : 2
    @newest = 2600
    @daemon = ContractDaemon.new { |received| reply(received) }
  end

  def reply(received)
    after = received.query.fetch("after", "0").to_i
    own_change_in_scan = @scan_condition == "role change on a later page"
    if received.path == "/v1/failovers/start"
      json_answer(failover_start("replica", this_machine: false))
    elsif received.path == "/v1/backup-rules/stop"
      json_answer("lease_pending" => false)
    elsif received.path == "/v1/databases/status"
      @role_reads += 1
      json_answer(APP_DATABASE)
    elsif received.query.key?("wait_millis")
      if after == 7
        return json_answer({ "code" => "cursor_expired", "message" => "events after 7 are no longer kept" },
                           410)
      end

      body = fixed_log_end_events(after, own_change_in_scan, @newest)
      sleep(0.05) if body["events"].empty?
      json_answer(body)
    elsif after == -1
      log_end = @log_ends.empty? ? @newest : @log_ends.shift
      # This database becomes a replica after the look stopped.
      @newest = 2601 if log_end == @newest && @role_reads == @want_role_reads
      json_answer("events" => [], "next" => log_end)
    elsif @scan_condition == "expired cursor" && @scans.zero?
      @scans += 1
      json_answer({ "code" => "cursor_expired", "message" => "pruned" }, 410)
    else
      json_answer(fixed_log_end_events(after, own_change_in_scan, @newest))
    end
  end
end

class FailoverTest < Minitest::Test
  def teardown
    Litebackup::Failover.event_wait_seconds = 10.0
    Litebackup::Failover.confirmation_limit_seconds = 15.0
    super
  end

  def test_stopping_following_awaits_a_running_callback_and_stops_callbacks
    fake = FailoverDaemon.new
    entered = Gate.new
    gate = Gate.new
    calls = []
    on_role_change = lambda do |role|
      calls << role
      entered.open!
      gate.wait(5)
    end
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      failover = client.failovers.start("/data/app.sqlite", on_role_change: on_role_change)
      assert(entered.wait(5))
      ending = Thread.new { failover.stop_following }
      refute(ending.join(0.2), "stop_following returned while a callback was running")
      gate.open!
      assert(ending.join(5))
      count = calls.length
      sleep(0.1)
      assert_equal(count, calls.length)
    end
  ensure
    gate.open!
    fake.daemon.close
  end

  def test_failover_stop_turns_off_callbacks_before_the_stop_request
    fake = FailoverDaemon.new
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      fake.failover = client.failovers.start("/data/app.sqlite")
      fake.failover.stop
    end
    assert_equal([true], fake.stopped_when_stop_arrived)
  ensure
    fake.daemon.close
  end

  def test_failover_sends_discard_local_only_when_asked
    fake = FailoverDaemon.new
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      fake.failover = client.failovers.start("/data/app.sqlite")
      fake.failover.stop
      fake.failover = client.failovers.start("/data/app.sqlite", discard_local: true)
      fake.failover.stop
    end
    assert_equal([{ "path" => "/data/app.sqlite" }, { "path" => "/data/app.sqlite", "discard_local" => true }],
                 fake.starts)
  ensure
    fake.daemon.close
  end

  def test_close_ends_a_failover_callback_waiting_on_the_client
    fake = FailoverDaemon.new
    fake.writer_gate = Gate.new
    entered = Gate.new
    started = Gate.new
    raised = []
    failover = nil
    client = Litebackup::Client.new(socket_path: fake.daemon.socket_path)
    on_role_change = lambda do |_role|
      # The start role's call could come before start returns and sets failover.
      started.wait(5)
      next if entered.open?

      entered.open!
      begin
        failover.writer
        raised << nil
      rescue Litebackup::Error => e
        raised << e
      end
    end
    failover = client.failovers.start("/data/app.sqlite", on_role_change: on_role_change)
    started.open!
    assert(entered.wait(5))
    sleep(0.1)
    closing = Thread.new { client.close }
    assert(closing.join(5), "close waited on a callback that waits on the client")
    assert_equal(1, raised.length)
    assert_equal("closed", raised.first&.code)
  ensure
    fake.writer_gate.open!
    fake.daemon.close
  end

  [0, 1].each do |failed_role_reads|
    define_method("test_failover_resumes_from_the_log_end_on_an_expired_cursor_#{failed_role_reads}") do
      fake = ExpiredCursorDaemon.new(failed_role_reads)
      heard = []
      followed = Gate.new
      on_role_change = lambda do |role|
        heard << role
        # The first replica is the start's; the second comes from the fresh cursor.
        followed.open! if heard.count("replica") == 2
      end
      Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
        failover = client.failovers.start("/data/app.sqlite", on_role_change: on_role_change,
                                                              on_error: ->(error) { heard << error.code })
        assert(followed.wait(5))
        sleep(0.2)
        assert_equal(fake.resume_expectations, [heard, fake.reads])
        failover.stop
      end
    ensure
      fake.daemon.close
    end
  end

  def test_failover_stopped_on_an_expired_cursor_reads_nothing_more
    fake = ExpiredCursorDaemon.new
    started = Gate.new
    stopped = Gate.new
    failover = nil
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      stop_on_error = lambda do |_error|
        started.wait(5)
        failover.stop
        stopped.open!
      end
      failover = client.failovers.start("/data/app.sqlite", on_error: stop_on_error)
      started.open!
      assert(stopped.wait(5))
      failover.join_follower
      assert_equal(["stop"], fake.reads)
    end
  ensure
    fake.daemon.close
  end

  def test_failover_reports_the_start_role_once_and_is_unknown_once_stopped
    fake = RoleDaemon.new
    roles = []
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      failover = client.failovers.start("/data/app.sqlite", on_role_change: ->(role) { roles << role })
      await_reported { roles == ["origin"] }
      sleep(0.2)
      assert_equal(["origin"], roles)
      failover.stop
      assert_equal("unknown", failover.role)
      refute(failover.origin?)
    end
  ensure
    fake.close
  end

  def test_failover_role_is_unknown_while_the_daemon_is_stopped
    fake = RoleDaemon.new
    roles = []
    errors = []
    refusals = []
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      failover = client.failovers.start("/data/app.sqlite", on_role_change: ->(role) { roles << role },
                                                            on_refused: ->(message) { refusals << message },
                                                            on_error: ->(error) { errors << error.code })
      await_reported { roles == ["origin"] }
      assert(failover.origin?)
      fake.daemon.stop
      stopped = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      await_reported { roles.length == 2 }
      assert_operator(Process.clock_gettime(Process::CLOCK_MONOTONIC) - stopped, :<, 1,
                      "the role becomes unknown at once")
      assert_equal(%w[origin unknown], roles)
      assert_equal("connection_failed", errors.first)
      fake.returned = true
      fake.daemon.serve
      await_reported { refusals == [REFUSED_MESSAGE] }
      sleep(0.2)
      # The role read reports origin; the older role change in the cursor's
      # page is skipped, and the older refusal is reported without changing
      # the role the read gave.
      assert_equal(%w[origin unknown origin], roles)
      assert(failover.origin?)
      # The log's end is read before the role read and again after it.
      assert_equal(["-1", "-1"], fake.log_end_cursors)
      failover.stop
    end
  ensure
    fake.close
  end

  ["answered errors", "no answer"].each do |long_polls|
    define_method("test_failover_role_the_daemon_does_not_confirm_is_unknown_after_the_limit_#{long_polls.tr(" ",
                                                                                                             "_")}") do
      Litebackup::Failover.event_wait_seconds = 0.1
      Litebackup::Failover.confirmation_limit_seconds = 0.4
      fake = RoleDaemon.new(long_polls)
      roles = []
      errors = []
      Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
        started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        failover = client.failovers.start("/data/app.sqlite", on_role_change: ->(role) { roles << role },
                                                              on_error: ->(error) { errors << error.code })
        await_reported { roles.length == 2 }
        elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
        assert_equal(%w[origin unknown], roles)
        assert((0.4...0.9).cover?(elapsed), "the role became unknown after #{elapsed.round(2)} s, want about 0.4")
        assert_equal(long_polls == "no answer" ? "timeout" : "unavailable", errors.first)
        fake.hang.open!
        failover.stop
      end
    ensure
      fake.close
    end
  end

  def test_failover_reads_its_role_again_when_the_log_shows_a_role_change_after_the_role_read
    fake = RoleDaemon.new
    fake.role_read = "replica"
    fake.raced_changes = { "events" => [role_event(10, "origin"), role_event(11, "replica")], "next" => 11 }
    roles = []
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      failover = client.failovers.start("/data/app.sqlite", on_role_change: ->(role) { roles << role })
      await_reported { roles == ["origin"] }
      fake.daemon.stop
      await_reported { roles.length == 2 }
      fake.returned = true
      fake.daemon.serve
      await_reported { roles.length == 3 }
      sleep(0.3)
      # The first role read raced the role changes, so it is read again; the
      # long-poll from the cursor reports neither.
      assert_equal(%w[origin unknown replica], roles)
      assert_equal(2, fake.role_reads)
      failover.stop
    end
  ensure
    fake.close
  end

  def test_failover_reads_its_role_again_when_the_log_shows_a_refusal_after_the_role_read
    fake = RoleDaemon.new
    fake.conflicted_from_read = 2
    refusal = { "id" => 10, "time" => "2026-01-01T00:02:00Z", "type" => "refused", "message" => REFUSED_MESSAGE,
                "name" => "app.sqlite" }
    fake.raced_changes = { "events" => [refusal], "next" => 10 }
    roles = []
    refusals = []
    Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
      failover = client.failovers.start("/data/app.sqlite", on_role_change: ->(role) { roles << role },
                                                            on_refused: ->(message) { refusals << message })
      await_reported { roles == ["origin"] }
      fake.daemon.stop
      await_reported { roles.length == 2 }
      fake.returned = true
      fake.daemon.serve
      await_reported { fake.role_reads >= 2 }
      sleep(0.3)
      # The first role read raced the refusal, so it is read again: no stale
      # origin, and each refusal once.
      assert_equal(%w[origin unknown], roles)
      refute(failover.origin?)
      assert_equal([REFUSED_MESSAGE, REFUSED_MESSAGE], refusals)
      failover.stop
    end
  ensure
    fake.close
  end

  ["unrelated events", "role change on a later page", "expired cursor"].each do |scan_condition|
    define_method("test_failover_look_past_the_log_end_pages_through_a_fixed_log_end_#{scan_condition.tr(" ", "_")}") do
      fake = FixedLogEndDaemon.new(scan_condition)
      roles = []
      Litebackup::Client.open(socket_path: fake.daemon.socket_path) do |client|
        failover = client.failovers.start("/data/app.sqlite", on_role_change: ->(role) { roles << role })
        await_reported { roles.length == 3 }
        sleep(0.2)
        # The change to replica after the fixed log end reaches the callback through the long-poll.
        assert_equal(%w[replica origin replica], roles)
        assert_equal(fake.want_role_reads, fake.role_reads)
        failover.stop
      end
    ensure
      fake.daemon.close
    end
  end
end

# The follower's decisions, without a daemon.
class FailoverStateTest < Minitest::Test
  def failover_state(on_role_change: nil, on_refused: nil)
    status = Litebackup::Models::Failover.from_wire("name" => "app.sqlite", "path" => "/data/app.sqlite",
                                                    "role" => "origin")
    Litebackup::Failover.new(Object.new, status, 7, on_role_change: on_role_change, on_refused: on_refused)
  end

  def event(fields)
    Litebackup::Event.from_wire(fields)
  end

  def test_a_read_a_closing_client_ended_clears_the_role_without_a_callback
    roles = []
    failover = failover_state(on_role_change: ->(role) { roles << role })
    progress = Litebackup::Failover::FollowProgress.new(7)
    # The client's close ends the calls that wait with closed before it stops following.
    assert_nil(failover.send(:retry_delay, Litebackup::Error.new("closed", "the client is closed"), progress))
    assert_equal("unknown", failover.role)
    assert_empty(roles)
  end

  def test_a_draining_event_makes_the_role_unknown_until_the_next_role_change
    roles = []
    failover = failover_state(on_role_change: ->(role) { roles << role })
    progress = Litebackup::Failover::FollowProgress.new(7)
    draining = { "id" => 8, "time" => "2026-01-01T00:00:00Z", "type" => "draining",
                 "message" => "this machine is draining", "name" => "app.sqlite" }
    failover.send(:apply_event, event(draining), progress)
    assert_equal(["unknown"], roles)
    refute(failover.origin?)
    failover.send(:apply_event, event(role_event(9, "origin")), progress)
    assert_equal(%w[unknown origin], roles)
    assert(failover.origin?)
  end

  def test_a_role_unknown_after_draining_is_read_again_after_a_quiet_long_poll
    failover = failover_state
    progress = Litebackup::Failover::FollowProgress.new(7)
    quiet = Litebackup::EventList.from_wire("events" => [], "next" => 8)
    failover.send(:read_unknown_role_again, quiet, progress)
    refute(progress.role_read_pending)
    draining = { "id" => 8, "time" => "2026-01-01T00:00:00Z", "type" => "draining",
                 "message" => "this machine is draining", "name" => "app.sqlite" }
    failover.send(:apply_event, event(draining), progress)
    failover.send(:read_unknown_role_again, Litebackup::EventList.from_wire("events" => [draining], "next" => 8),
                  progress)
    refute(progress.role_read_pending)
    failover.send(:read_unknown_role_again, quiet, progress)
    assert(progress.role_read_pending)
    assert(progress.log_end_pending)
  end

  def test_a_role_read_while_the_daemon_drains_reports_unknown
    failover = failover_state
    draining = APP_DATABASE.merge("draining" => true)
    assert_equal("unknown", failover.send(:status_role, Litebackup::DatabaseStatus.from_wire(draining)))
    assert_equal("origin", failover.send(:status_role, Litebackup::DatabaseStatus.from_wire(APP_DATABASE)))
    # A refused file reads as conflicted, which makes the role unknown too.
    refused = APP_DATABASE.merge("role" => "replica", "state" => "conflicted")
    assert_equal("unknown", failover.send(:status_role, Litebackup::DatabaseStatus.from_wire(refused)))
  end

  def test_a_role_callback_that_stops_the_failover_hears_no_refusal
    refusals = []
    failover = nil
    stop_on_unknown = ->(role) { failover.send(:mark_stopped) if role == "unknown" }
    failover = failover_state(on_role_change: stop_on_unknown, on_refused: ->(message) { refusals << message })
    refusal = { "id" => 9, "time" => "2026-01-01T00:01:00Z", "type" => "refused", "message" => REFUSED_MESSAGE,
                "name" => "app.sqlite" }
    failover.send(:apply_event, event(refusal), Litebackup::Failover::FollowProgress.new(7))
    assert_empty(refusals)
    assert_equal("unknown", failover.role)
  end

  # A process forked after the failover started has no follower, so it
  # never reports the role it inherited.
  def test_a_forked_failover_reports_an_unknown_role
    failover = failover_state
    assert(failover.origin?)
    reader, writer = IO.pipe
    pid = fork do
      reader.close
      writer.write("#{failover.role} #{failover.origin?}")
      writer.close
      exit!(0)
    end
    writer.close
    Process.wait(pid)
    assert_equal("unknown false", reader.read)
    assert(failover.origin?)
  end
end
