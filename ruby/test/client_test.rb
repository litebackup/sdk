# frozen_string_literal: true

require_relative "test_helper"
require "pathname"

INVITE = "litebackup_invite_exampleinvitevalue@backups.example.com"
JOIN_TOKEN = "litebackup_join_exampletokenvalue@backups.example.com"
API_KEY = "litebackup_examplekeyvalue@backups.example.com"

# A unix socket server whose connections a test answers byte by byte, for
# what ContractDaemon never sends: chunks, truncated bodies, slow answers,
# and connections closed without an answer.
class RawDaemon
  attr_reader :socket_path, :requests, :connections

  # answer is called with the connection, the request line, and how many
  # requests came before it on every connection.
  def initialize(&answer)
    @directory = Dir.mktmpdir("lbraw-")
    @socket_path = File.join(@directory, "client.sock")
    @server = UNIXServer.new(@socket_path)
    @answer = answer
    @requests = []
    @connections = 0
    @lock = Mutex.new
    @acceptor = Thread.new do
      loop do
        connection = @server.accept
        @lock.synchronize { @connections += 1 }
        Thread.new(connection) { |accepted| serve(accepted) }
      end
    rescue IOError, SystemCallError
      nil
    end
  end

  def serve(connection)
    loop do
      line = connection.gets("\r\n")
      break if line.nil?

      length = 0
      while (header = connection.gets("\r\n")) && header != "\r\n"
        length = header.split(":", 2).last.to_i if header.downcase.start_with?("content-length")
      end
      connection.read(length)
      count = @lock.synchronize do
        @requests << line.strip
        @requests.length - 1
      end
      break unless @answer.call(connection, line.strip, count)
    end
  rescue IOError, SystemCallError
    nil
  ensure
    connection.close
  end

  def close
    @server.close
    @acceptor.join
    FileUtils.rm_rf(@directory)
  end
end

STATUS_ANSWER = ContractDaemon::STATUS_BODY

def write_answer(connection, body, headers = {})
  head = ["HTTP/1.1 200 OK", "Litebackup-Client-Version: 1.2.0", "Content-Type: application/json"]
  head += headers.map { |name, value| "#{name}: #{value}" }
  head << "Content-Length: #{body.bytesize}" unless headers.key?("Transfer-Encoding")
  connection.write("#{head.join("\r\n")}\r\n\r\n#{body}")
end

class TransportTest < Minitest::Test
  def transport_to(daemon)
    Litebackup::Transport.new(daemon.socket_path)
  end

  def test_keep_alive_reuses_one_connection
    accepted = 0
    daemon = RawDaemon.new do |connection, _line, count|
      accepted += 1 if count.zero?
      write_answer(connection, count.zero? ? STATUS_ANSWER : '{"rules":[]}')
      true
    end
    client = Litebackup::Client.new(socket_path: daemon.socket_path)
    3.times { client.rules.list }
    assert_equal(4, daemon.requests.length)
    assert_equal(1, daemon.connections)
  ensure
    client&.close
    daemon&.close
  end

  def test_a_chunked_answer_is_read_whole
    daemon = RawDaemon.new do |connection, _line, _count|
      connection.write("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nLitebackup-Client-Version: 1.2.0\r\n\r\n")
      ['{"rules"', ":[]}"].each { |piece| connection.write("#{piece.bytesize.to_s(16)}\r\n#{piece}\r\n") }
      connection.write("0\r\n\r\n")
      true
    end
    answer = transport_to(daemon).request("GET", "/v1/backup-rules/list", nil, 5)
    assert_equal([200, "1.2.0", '{"rules":[]}'], [answer.status, answer.client_version, answer.body])
  ensure
    daemon&.close
  end

  def test_a_truncated_answer_is_connection_failed
    daemon = RawDaemon.new do |connection, _line, _count|
      connection.write("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"rules\"")
      false
    end
    error = assert_raises(Litebackup::Error) { transport_to(daemon).request("POST", "/v1/x", "{}", 5) }
    assert_equal("connection_failed", error.code)
  ensure
    daemon&.close
  end

  def test_an_answer_past_the_limit_is_invalid_response
    daemon = RawDaemon.new do |connection, _line, _count|
      connection.write("HTTP/1.1 200 OK\r\nContent-Length: #{(64 << 20) + 1}\r\n\r\n")
      false
    end
    error = assert_raises(Litebackup::Error) { transport_to(daemon).request("GET", "/v1/x", nil, 5) }
    assert_equal("invalid_response", error.code)
  ensure
    daemon&.close
  end

  def test_a_trickling_answer_ends_with_the_whole_call_deadline
    daemon = RawDaemon.new do |connection, _line, _count|
      connection.write("HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
      100.times do
        connection.write("x")
        sleep(0.05)
      end
      false
    end
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    error = assert_raises(Litebackup::Error) { transport_to(daemon).request("GET", "/v1/x", nil, 0.3) }
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
    assert_equal("timeout", error.code)
    assert_equal(0, error.status)
    assert_operator(elapsed, :<, 1.0, "each byte reset the deadline")
  ensure
    daemon&.close
  end

  def test_an_answer_without_a_chunk_size_is_connection_failed
    daemon = RawDaemon.new do |connection, _line, _count|
      connection.write("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n\r\n")
      false
    end
    error = assert_raises(Litebackup::Error) { transport_to(daemon).request("POST", "/v1/x", "{}", 5) }
    assert_equal("connection_failed", error.code)
  ensure
    daemon&.close
  end

  # An answer whose bytes are always ready still ends at the whole-call deadline.
  def test_a_continuously_readable_answer_ends_with_the_deadline
    daemon = RawDaemon.new do |connection, _line, _count|
      connection.write("HTTP/1.1 200 OK\r\nContent-Length: #{60 << 20}\r\n\r\n")
      piece = "x" * 65_536
      400.times do
        connection.write(piece)
        sleep(0.005)
      end
      false
    end
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    error = assert_raises(Litebackup::Error) { transport_to(daemon).request("GET", "/v1/x", nil, 0.3) }
    assert_equal("timeout", error.code)
    assert_operator(Process.clock_gettime(Process::CLOCK_MONOTONIC) - started, :<, 1.0)
  ensure
    daemon&.close
  end

  # A thread killed while it waits gives its connection back closed.
  def test_a_killed_call_leaves_no_active_connection
    gate = Gate.new
    daemon = ContractDaemon.new do
      gate.wait(10)
      json_answer("events" => [], "next" => 0)
    end
    transport = Litebackup::Transport.new(daemon.socket_path)
    waiting = Thread.new { transport.request("GET", "/v1/events/list", nil, nil) }
    await_reported { daemon.requests.length == 1 }
    waiting.kill
    waiting.join
    assert_empty(transport.instance_variable_get(:@active))
  ensure
    gate&.open!
    transport&.close
    daemon&.close
  end

  # Closing a client inherited by a forked child leaves the parent's call alone.
  def test_closing_a_client_in_a_forked_child_leaves_the_parents_calls
    gate = Gate.new
    daemon = ContractDaemon.new do
      gate.wait(10)
      json_answer("name" => "a.sqlite", "path" => "/a", "role" => "origin", "state" => "synced")
    end
    client = Litebackup::Client.new(socket_path: daemon.socket_path)
    client.connect
    syncing = Thread.new { client.databases.sync("a.sqlite") }
    await_reported { daemon.requests.length == 1 }
    pid = fork do
      client.close
      exit!(0)
    end
    Process.wait(pid)
    gate.open!
    assert_equal("synced", syncing.value.state)
  ensure
    gate&.open!
    client&.close
    daemon&.close
  end

  def test_concurrent_long_polls_each_use_their_own_connection
    daemon = ContractDaemon.new do
      sleep(0.4)
      json_answer("events" => [], "next" => 0)
    end
    client = Litebackup::Client.new(socket_path: daemon.socket_path)
    client.connect
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    threads = Array.new(3) { Thread.new { client.events.page(wait: 1) } }
    threads.each(&:join)
    assert_operator(Process.clock_gettime(Process::CLOCK_MONOTONIC) - started, :<, 1.0)
  ensure
    client&.close
    daemon&.close
  end

  # A daemon that closes each reused connection without answering, after the first answer on it.
  def closing_daemon(attempts)
    RawDaemon.new do |connection, _line, count|
      if count.zero?
        write_answer(connection, '{"rules":[]}')
        true
      else
        attempts << count
        false
      end
    end
  end

  def test_a_get_on_a_reused_connection_is_sent_once_more_and_only_once
    attempts = []
    daemon = closing_daemon(attempts)
    transport = transport_to(daemon)
    transport.request("GET", "/v1/backup-rules/list", nil, 5)
    error = assert_raises(Litebackup::Error) { transport.request("GET", "/v1/backup-rules/list", nil, 5) }
    assert_equal("connection_failed", error.code)
    assert_equal(2, attempts.length, "the GET was sent #{attempts.length} times after the first answer")
  ensure
    daemon&.close
  end

  def test_a_post_on_a_reused_connection_is_never_sent_twice
    attempts = []
    daemon = closing_daemon(attempts)
    transport = transport_to(daemon)
    transport.request("GET", "/v1/backup-rules/list", nil, 5)
    error = assert_raises(Litebackup::Error) { transport.request("POST", "/v1/databases/sync", "{}", 5) }
    assert_equal("connection_failed", error.code)
    assert_equal(1, attempts.length)
  ensure
    daemon&.close
  end

  def test_close_ends_a_pending_call
    gate = Gate.new
    daemon = ContractDaemon.new do
      gate.wait(10)
      json_answer("events" => [], "next" => 0)
    end
    client = Litebackup::Client.new(socket_path: daemon.socket_path)
    client.connect
    waiting = Thread.new do
      client.events.page(wait: 5)
    rescue Litebackup::Error => e
      e
    end
    sleep(0.2)
    client.close
    assert(waiting.join(5), "close did not end the call")
    assert_equal("closed", waiting.value.code)
  ensure
    gate&.open!
    daemon&.close
  end

  def test_close_fails_later_calls_and_never_shuts_down
    daemon = ContractDaemon.new { json_answer("rules" => []) }
    client = Litebackup::Client.new(socket_path: daemon.socket_path)
    client.rules.list
    client.close
    assert_equal("closed", assert_raises(Litebackup::Error) { client.rules.list }.code)
    assert_equal(["/v1/backup-rules/list"], daemon.requests.map(&:path))
  ensure
    daemon&.close
  end

  def test_a_client_made_before_a_fork_is_refused_in_the_child
    daemon = ContractDaemon.new { json_answer("rules" => []) }
    client = Litebackup::Client.new(socket_path: daemon.socket_path)
    client.rules.list
    reader, writer = IO.pipe
    pid = fork do
      reader.close
      begin
        client.rules.list
        writer.write("no error")
      rescue Litebackup::Error => e
        writer.write(e.code)
      end
      writer.close
      exit!(0)
    end
    writer.close
    Process.wait(pid)
    assert_equal("invalid_argument", reader.read)
    # The parent's connection is unaffected.
    assert_equal([], client.rules.list)
  ensure
    client&.close
    daemon&.close
  end
end

# Arguments the SDK checks or leaves out before anything is sent, by value
# rather than by truthiness.
class ArgumentTest < Minitest::Test
  include WorkingDirectory

  def test_zero_durations_and_defaults_are_left_out
    call = Litebackup::Wire.acquire("a.sqlite", timeout: 0, lock_timeout: 0.0, umask: 0)
    assert_equal({ "name" => "a.sqlite", "umask" => 0 }, call.body)
    assert_nil(call.timeout_seconds)
    assert_equal({ "name" => "a.sqlite" }, Litebackup::Wire.release("a.sqlite", timeout: 0).body)
    assert_equal({}, Litebackup::Wire.events_page(after: 0, wait: 0, limit: 0).query)
    assert_equal({}, Litebackup::Wire.shutdown(timeout: 0).body)
  end

  def test_an_empty_after_and_a_zero_limit
    assert_equal({ "after" => "", "limit" => "100" }, Litebackup::Wire.status(after: "").query)
    assert_equal("invalid_argument", assert_raises(Litebackup::Error) { Litebackup::Wire.status(limit: 0) }.code)
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.databases_page(limit: 1001) }.code)
    assert_equal("invalid_argument", assert_raises(Litebackup::Error) { Litebackup::Wire.status(limit: true) }.code)
  end

  def test_a_zero_versions_limit_sends_nothing
    daemon = ContractDaemon.new { json_answer("versions" => [], "next" => 0) }
    Litebackup::Client.open(socket_path: daemon.socket_path) do |client|
      assert_empty(client.databases.versions("a.sqlite", limit: 0))
    end
    assert_empty(daemon.requests)
  ensure
    daemon&.close
  end

  def test_an_empty_api_key_is_no_credential
    environment = { Litebackup::API_KEY_ENVIRONMENT => JOIN_TOKEN }
    settings = Litebackup::Client::Settings.new(api_key: "", session: nil, socket_path: "/tmp/x.sock",
                                                binary_path: nil, launch: false, environment: environment)
    assert_equal("", settings.api_key)
    assert_equal("", settings.foreground_environment[Litebackup::API_KEY_ENVIRONMENT])
  end

  def test_a_zero_umask_is_sent_and_one_past_0777_is_refused
    assert_equal(0, Litebackup::Wire.restore("a.sqlite", path: "b.sqlite", umask: 0).body["umask"])
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.restore("a", path: "b", umask: 0o1000) }.code)
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.restore("a", path: "b", umask: -1) }.code)
  end

  def test_infinite_durations_are_refused
    error = assert_raises(Litebackup::Error) { Litebackup::Wire.release("a", timeout: Float::INFINITY) }
    assert_equal("invalid_argument", error.code)
  end

  def test_negative_and_non_numeric_durations_are_refused
    assert_equal("invalid_argument", assert_raises(Litebackup::Error) do
      Litebackup::Wire.release("a", timeout: -1)
    end.code)
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.release("a", timeout: "5s") }.code)
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.release("a", timeout: Float::NAN) }.code)
  end

  def test_paths_are_absolute_and_cleaned_lexically
    assert_equal(File.join(@working_directory, "b.sqlite"), Litebackup::Wire.absolute_path("a/../b.sqlite"))
    assert_equal(File.join(@working_directory, "a.sqlite"), Litebackup::Wire.absolute_path(Pathname.new("./a.sqlite")))
    assert_equal("/data/b.sqlite", Litebackup::Wire.absolute_path("/data//x/../b.sqlite"))
    # A ~ is a name, not the home directory.
    assert_equal(File.join(@working_directory, "~", "a.sqlite"), Litebackup::Wire.absolute_path("~/a.sqlite"))
    assert_equal("invalid_argument", assert_raises(Litebackup::Error) { Litebackup::Wire.absolute_path(7) }.code)
  end

  def test_times_are_utc_with_fractions_only_when_set
    assert_equal("2026-11-15T09:30:00Z", Litebackup::Wire.wire_time(Time.new(2026, 11, 15, 10, 30, 0, "+01:00")))
    assert_equal("2026-11-15T09:30:00.25Z", Litebackup::Wire.wire_time("2026-11-15T09:30:00.250Z"))
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.wire_time("2026-11-15 09:30") }.code)
  end

  def test_a_name_and_a_prefix_together_are_refused_but_empty_strings_are_not_set
    assert_equal("invalid_argument",
                 assert_raises(Litebackup::Error) { Litebackup::Wire.start_backup("a", name: "x", prefix: "y") }.code)
    body = Litebackup::Wire.start_backup("a", name: "", prefix: "y", maximum_local_databases: 0).body
    assert_equal({ "path" => File.join(@working_directory, "a"), "prefix" => "y" }, body)
  end

  def test_page_limits_outside_1_to_1000_are_refused_before_sending
    daemon = ContractDaemon.new { json_answer({}) }
    Litebackup::Client.open(socket_path: daemon.socket_path) do |client|
      [0, 1001, -1].each do |limit|
        assert_equal("invalid_argument", assert_raises(Litebackup::Error) { client.databases.page(limit: limit) }.code)
        assert_equal("invalid_argument", assert_raises(Litebackup::Error) { client.session.status(limit: limit) }.code)
      end
    end
    assert_empty(daemon.requests)
  ensure
    daemon&.close
  end

  def test_stopping_with_an_empty_name_or_id_sends_nothing
    daemon = ContractDaemon.new { json_answer({}) }
    Litebackup::Client.open(socket_path: daemon.socket_path) do |client|
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) { client.backups.stop("") }.code)
      assert_equal("invalid_argument", assert_raises(Litebackup::Error) { client.rules.stop("") }.code)
    end
    assert_empty(daemon.requests)
  ensure
    daemon&.close
  end

  def test_the_umask_reads_from_proc_or_sets_and_restores_it
    previous = File.umask(0o027)
    assert_equal(0o027, Litebackup::Umask.current_umask)
    assert_equal(0o027, File.umask)
    path = File.join(@working_directory, "status")
    File.write(path, "Name:\t\xff\nUmask:\t0022\n".b)
    assert_equal(0o022, Litebackup::Umask.status_umask(path))
    File.write(path, "Name:\tx\n")
    assert_nil(Litebackup::Umask.status_umask(path))
  ensure
    File.umask(previous)
  end
end

class LaunchTest < Minitest::Test
  include WorkingDirectory

  def executable(path, script = "exit 0")
    FileUtils.mkdir_p(File.dirname(path))
    File.write(path, "#!/bin/sh\n#{script}\n")
    File.chmod(0o755, path)
    path
  end

  def environment(directory = @working_directory)
    { "PATH" => ENV.fetch("PATH", ""), "HOME" => directory, "XDG_RUNTIME_DIR" => File.join(directory, "run") }
  end

  def test_explicit_socket_never_launches
    Litebackup::Client.open(socket_path: File.join(@working_directory, "missing.sock"),
                            binary_path: "/bin/false") do |client|
      assert_equal("connection_failed", assert_raises(Litebackup::Error) { client.rules.list }.code)
    end
  end

  def test_binary_resolution_order
    on_path = File.join(@working_directory, "path")
    FileUtils.mkdir_p(on_path)
    home = File.join(@working_directory, "home")
    local = executable(File.join(home, ".local", "bin", "litebackup"))
    search = { "PATH" => on_path, "HOME" => home }
    assert_equal(local, Litebackup::Binary.litebackup_binary(search))
    found = executable(File.join(on_path, "litebackup"))
    assert_equal(found, Litebackup::Binary.litebackup_binary(search))
    override = executable(File.join(@working_directory, "opt", "litebackup"))
    explicit = executable(File.join(@working_directory, "explicit", "litebackup"))
    assert_equal(override, Litebackup::Binary.litebackup_binary(search.merge("LITEBACKUP_PATH" => override)))
    assert_equal(explicit, Litebackup::Binary.litebackup_binary(search.merge("LITEBACKUP_PATH" => override), explicit))
    error = assert_raises(Litebackup::Error) do
      Litebackup::Binary.litebackup_binary("PATH" => File.join(@working_directory, "none"),
                                           "HOME" => File.join(@working_directory, "nohome"))
    end
    assert_equal("binary_not_found", error.code)
    assert_includes(error.message, "cli.sh")
  end

  def test_the_plain_gem_bundles_no_binary
    assert_nil(Litebackup::Binary.bundled_binary) unless File.exist?(Litebackup::Binary::BUNDLED_PATH)
  end

  def test_missing_binary_is_reported_on_connect
    search = { "PATH" => @working_directory, "HOME" => @working_directory,
               "XDG_RUNTIME_DIR" => File.join(@working_directory, "run") }
    Litebackup::Client.open(session: "nobinary", environment: search) do |client|
      assert_equal("binary_not_found", assert_raises(Litebackup::Error) { client.connect }.code)
    end
  end

  def test_session_sockets
    missing = File.join(@working_directory, "client.json")
    runtime = { "XDG_RUNTIME_DIR" => "/run/user/7", "HOME" => @working_directory }
    assert_equal(["default", "/run/user/7/litebackup/default/client.sock"],
                 Litebackup::Paths.session_socket(nil, runtime, missing))
    assert_equal("jobs", Litebackup::Paths.session_socket(
      "", { "LITEBACKUP_SESSION" => "jobs", "XDG_RUNTIME_DIR" => "/r", "HOME" => @working_directory }, missing
    ).first)
    service = File.join(@working_directory, ".config", "litebackup")
    FileUtils.mkdir_p(service)
    File.write(File.join(service, "service.json"), JSON.generate("runtime_directory" => "/var/run/litebackup-user"))
    assert_equal("/var/run/litebackup-user/web/client.sock",
                 Litebackup::Paths.session_socket("web", { "HOME" => @working_directory }, missing).last)
    system = File.join(@working_directory, "system.json")
    File.write(system, JSON.generate("uid" => Process.uid, "runtime_directory" => "/run/litebackup"))
    assert_equal("/run/litebackup/web/client.sock",
                 Litebackup::Paths.session_socket("web", { "HOME" => @working_directory }, system).last)
    assert_raises(Litebackup::Error) { Litebackup::Paths.session_socket("Bad Name", {}, missing) }
  end

  def test_session_socket_refuses_a_shared_runtime_directory
    shared = File.join(@working_directory, "litebackup-#{Process.uid}")
    Dir.mkdir(shared, 0o777)
    File.chmod(0o777, shared)
    error = assert_raises(Litebackup::Error) do
      Litebackup::Paths.session_socket("web", { "HOME" => @working_directory, "TMPDIR" => @working_directory },
                                       File.join(@working_directory, "none.json"))
    end
    assert_includes(error.message, "this user's alone")
  end

  def test_invite_credentials_are_refused_without_repeating_them
    [{ api_key: INVITE }, { environment: { Litebackup::API_KEY_ENVIRONMENT => INVITE } }].each do |arguments|
      error = assert_raises(Litebackup::Error) { Litebackup::Client.new(socket_path: "/tmp/x.sock", **arguments) }
      assert_equal("invalid_argument", error.code)
      assert_includes(error.message, "invite token")
      refute_includes(error.message, "exampleinvitevalue")
    end
  end

  # A litebackup stand-in that appends its command and the
  # LITEBACKUP_API_KEY it got ("unset" without one) to a file.
  def credential_stand_in(directory)
    calls = File.join(directory, "calls")
    binary = executable(File.join(directory, "litebackup"),
                        "printf '%s %s\\n' \"$1\" \"${LITEBACKUP_API_KEY-unset}\" >> '#{calls}'")
    [binary, calls]
  end

  # Each connect: LITEBACKUP_API_KEY, the api_key argument, and what launch
  # got. Without either the daemon uses the key it saved, and an empty
  # api_key is no credential, whatever the variable holds.
  [
    [nil, JOIN_TOKEN, "launch #{JOIN_TOKEN}"],
    [JOIN_TOKEN, API_KEY, "launch #{API_KEY}"],
    [API_KEY, nil, "launch #{API_KEY}"],
    [nil, nil, "launch unset"],
    [JOIN_TOKEN, "", "launch "]
  ].each_with_index do |(variable, api_key, launched), index|
    define_method("test_launch_passes_the_credential_#{index}") do
      binary, calls = credential_stand_in(@working_directory)
      search = environment
      search[Litebackup::API_KEY_ENVIRONMENT] = variable unless variable.nil?
      Litebackup::Client.open(session: "unit", binary_path: binary, environment: search, api_key: api_key) do |client|
        assert_raises(Litebackup::Error) { client.connect }
      end
      assert_equal([launched], File.read(calls).lines(chomp: true))
    end
  end

  def test_foreground_backup_passes_the_api_key
    binary, calls = credential_stand_in(@working_directory)
    search = environment.merge(Litebackup::API_KEY_ENVIRONMENT => JOIN_TOKEN)
    Litebackup::Client.open(session: "unit", binary_path: binary, environment: search, api_key: API_KEY) do |client|
      client.backups.once(File.join(@working_directory, "app.sqlite"))
    end
    assert_equal(["backup #{API_KEY}"], File.read(calls).lines(chomp: true))
  end

  def test_launch_failed_carries_the_command_message
    arguments = File.join(@working_directory, "arguments")
    binary = executable(File.join(@working_directory, "litebackup"), <<~SCRIPT)
      echo "$@" > '#{arguments}'
      echo '{"event":"warning","code":"not_configured","message":"the server waits for setup"}' >&2
      echo '{"code":"unauthenticated","message":"this machine has not joined a server"}' >&2
      exit 1
    SCRIPT
    Litebackup::Client.open(session: "unit", binary_path: binary, environment: environment) do |client|
      error = assert_raises(Litebackup::Error) { client.connect }
      assert_equal("launch_failed", error.code)
      assert_equal("starting Litebackup failed: this machine has not joined a server", error.message)
    end
    assert_equal("launch --session unit --stdout-format json\n", File.read(arguments))
    assert_equal(["launch_failed", "plain text"], Litebackup::Launch.launch_refusal("plain text\n"))
  end

  def test_a_launch_that_refuses_a_new_credential_keeps_drain_required
    message = "drain it first with litebackup drain, then change the key"
    binary = executable(File.join(@working_directory, "litebackup"),
                        "echo '{\"code\":\"drain_required\",\"message\":\"#{message}\"}' >&2\nexit 1")
    Litebackup::Client.open(session: "unit", binary_path: binary, environment: environment) do |client|
      error = assert_raises(Litebackup::Error) { client.connect }
      assert_equal("drain_required", error.code)
      assert_equal("starting Litebackup failed: #{message}", error.message)
    end
  end

  def pid_alive?(pid)
    Process.kill(0, pid)
    true
  rescue Errno::ESRCH
    false
  end

  def started_pid(pid_file)
    500.times do
      text = File.exist?(pid_file) ? File.read(pid_file).strip : ""
      return text.to_i unless text.empty?

      sleep(0.01)
    end
    flunk("the job did not start")
  end

  # A litebackup stub that writes its PID and sleeps, optionally ignoring SIGTERM.
  def sleeping_binary(ignore_term: false)
    pid_file = File.join(@working_directory, "job.pid")
    trap = ignore_term ? "trap '' TERM\n" : ""
    body = ignore_term ? "while :; do sleep 0.05; done" : "exec sleep 30"
    [executable(File.join(@working_directory, "litebackup"), "#{trap}echo $$ > '#{pid_file}'\n#{body}"), pid_file]
  end

  def test_once_on_a_closed_client_starts_nothing
    binary, pid_file = sleeping_binary
    client = Litebackup::Client.new(session: "jobs", binary_path: binary, environment: environment)
    client.close
    assert_equal("closed", assert_raises(Litebackup::Error) { client.backups.once("app.sqlite") }.code)
    refute_path_exists(pid_file)
  end

  def test_close_ends_a_running_foreground_backup
    binary, pid_file = sleeping_binary
    client = Litebackup::Client.new(session: "jobs", binary_path: binary, environment: environment)
    backing_up = Thread.new do
      client.backups.once("app.sqlite")
    rescue Litebackup::Error => e
      e
    end
    pid = started_pid(pid_file)
    client.close
    assert(backing_up.join(5))
    assert_equal("closed", backing_up.value.code)
    refute(pid_alive?(pid))
  end

  def test_once_stops_after_its_timeout_and_grace
    Litebackup::Foreground.deadline_grace_seconds = 0.2
    Litebackup::Foreground.stop_grace_seconds = 0.2
    binary, pid_file = sleeping_binary(ignore_term: true)
    Litebackup::Client.open(session: "jobs", binary_path: binary, environment: environment) do |client|
      assert_equal("timeout", assert_raises(Litebackup::Error) { client.backups.once("app.sqlite", timeout: 0.1) }.code)
    end
    refute(pid_alive?(File.read(pid_file).to_i))
  ensure
    Litebackup::Foreground.deadline_grace_seconds = Litebackup::Wire::DEADLINE_GRACE_SECONDS
    Litebackup::Foreground.stop_grace_seconds = Litebackup::Foreground::STOP_GRACE_SECONDS
  end

  def test_once_returns_when_a_child_the_job_started_keeps_its_output_open
    binary = executable(File.join(@working_directory, "litebackup"), "sleep 30 &\nexit 0")
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    Litebackup::Client.open(session: "jobs", binary_path: binary, environment: environment) do |client|
      client.backups.once("app.sqlite")
    end
    assert_operator(Process.clock_gettime(Process::CLOCK_MONOTONIC) - started, :<, 5)
  end

  def test_a_failed_job_lists_every_file_diagnostic_once
    script = <<~SCRIPT
      echo 'log line' >&2
      echo '{"code":"busy","message":"a.sqlite is written by worker-2"}' >&2
      echo '{"code":"busy","message":"a.sqlite is written by worker-2"}' >&2
      echo '{"code":"busy","message":"b.sqlite is written by worker-3"}' >&2
      echo '{"code":"busy","message":"2 databases were not backed up"}' >&2
      exit 1
    SCRIPT
    binary = executable(File.join(@working_directory, "litebackup"), script)
    Litebackup::Client.open(session: "jobs", binary_path: binary, environment: environment) do |client|
      error = assert_raises(Litebackup::Error) { client.backups.once("app.sqlite") }
      assert_equal("busy", error.code)
      assert_equal("2 databases were not backed up\na.sqlite is written by worker-2\nb.sqlite is written by worker-3",
                   error.message)
    end
  end

  def test_once_sends_its_command_line
    arguments = File.join(@working_directory, "arguments")
    binary = executable(File.join(@working_directory, "litebackup"), "echo \"$@\" > '#{arguments}'")
    Litebackup::Client.open(session: "jobs", binary_path: binary, environment: environment) do |client|
      client.backups.once("data/app.sqlite", prefix: "app", durability: "cloud", timeout: 1.5)
    end
    assert_equal("backup --stdout-format json --durability cloud --session jobs --timeout 1500ms -- " \
                 "#{@working_directory}/data/app.sqlite app/\n", File.read(arguments))
  end
end

class ReleaseTest < Minitest::Test
  def metadata(name)
    File.read(File.join(CONTRACT_ROOT, "binary", name))
  end

  def test_release_metadata
    release = Litebackup::Release.find_release(metadata("package-metadata.json"), version: "0.3.30")
    assert_equal("0.3.30", release.version)
    assert_equal("litebackup-linux-amd64", release.file("litebackup-linux-amd64").filename)
    assert_raises(Litebackup::Release::NotListed) { release.file("missing") }
    assert_raises(Litebackup::Release::SignatureError) do
      Litebackup::Release.find_release(metadata("package-metadata-tampered.json"), version: "0.3.30")
    end
    assert_raises(Litebackup::Release::NotListed) do
      Litebackup::Release.find_release(metadata("package-metadata.json"), version: "9.9.9")
    end
  end
end
