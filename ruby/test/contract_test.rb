# frozen_string_literal: true

require_relative "test_helper"
require "pathname"

# Runs the language-neutral fixtures in testdata/contract, which the Go,
# TypeScript, and Python SDKs run too. The lifecycle fixtures that cancel run
# only against async clients, and Ruby has none, as Python's sync client has none.
module ContractFixtures
  module_function

  def names(fixture_directory)
    Dir[File.join(CONTRACT_ROOT, fixture_directory, "*.json")].map { |path| File.basename(path, ".json") }.sort
  end

  # This process's umask, read by setting it and putting it back, apart from how the SDK reads it.
  def fixture_umask
    previous = File.umask(0)
    File.umask(previous)
    previous
  end

  def read(fixture_directory, name, cwd = "/")
    text = File.read(File.join(CONTRACT_ROOT, fixture_directory, "#{name}.json"))
    substituted = text.gsub("{cwd}", JSON.generate(cwd)[1...-1]).gsub('"{umask}"', fixture_umask.to_s)
    JSON.parse(substituted)
  end

  # A fixture duration such as 1500ms or -1s, in seconds.
  def duration(text)
    return nil if text.nil?

    match = /\A(-?\d+(?:\.\d+)?)(ms|s|m|h)\z/.match(text)
    raise "bad duration #{text}" if match.nil?

    match[1].to_f * { "ms" => 0.001, "s" => 1, "m" => 60, "h" => 3600 }.fetch(match[2])
  end

  # {"$path": ...} becomes a relative Pathname, which resolves against the working directory.
  def path_argument(value)
    value.is_a?(Hash) && value.key?("$path") ? Pathname.new(value["$path"]) : value
  end

  # The group, method, and arguments of a fixture operation in the Ruby API.
  def operation_call(operation, args)
    keywords = {}
    pass_keywords = lambda do |*field_names, convert: ->(value) { value }|
      field_names.each { |field| keywords[field.to_sym] = convert.call(args[field]) if args.key?(field) }
    end
    group, method_name = operation.split(".", 2)
    positional = []
    time = ->(text) { Time.iso8601(text) }
    case operation
    when "session.status", "databases.page"
      pass_keywords.call("after", "limit")
    when "session.shutdown"
      pass_keywords.call("durability")
      pass_keywords.call("timeout", convert: method(:duration))
    when "databases.get", "databases.sync", "databases.writer", "backups.stop", "replicas.stop"
      positional = [args["name"]]
    when "databases.offload"
      positional = [args["name"]]
      pass_keywords.call("durability")
    when "databases.restore"
      positional = [args["name"]]
      keywords[:path] = path_argument(args["path"])
      pass_keywords.call("version", "umask")
      pass_keywords.call("time", convert: time)
    when "databases.revert"
      positional = [args["name"]]
      pass_keywords.call("version")
      pass_keywords.call("time", convert: time)
    when "databases.acquire"
      positional = [args["name"]]
      pass_keywords.call("path", convert: method(:path_argument))
      pass_keywords.call("maintenance", "offload_on_release", "offload_durability", "machine_label", "discard_local",
                         "umask")
      pass_keywords.call("timeout", "lock_timeout", convert: method(:duration))
    when "databases.release"
      positional = [args["name"]]
      pass_keywords.call("durability", "offload")
      pass_keywords.call("timeout", convert: method(:duration))
    when "backups.start"
      positional = [path_argument(args["source"])]
      pass_keywords.call("name", "prefix", "maximum_local_databases", "offload_durability", "offload_rollback_journal")
    when "rules.stop"
      positional = [args["id"]]
    when "replicas.start"
      positional = [args["name"]]
      pass_keywords.call("path", convert: method(:path_argument))
      pass_keywords.call("write_protection")
      pass_keywords.call("timeout", convert: method(:duration))
    when "replicas.start_prefix"
      positional = [args["prefix"]]
      keywords[:directory] = args["directory"]
      pass_keywords.call("write_protection")
    when "replicas.stop_prefix"
      positional = [args["prefix"]]
      keywords[:directory] = args["directory"]
    when "events.page"
      pass_keywords.call("after", "limit")
      pass_keywords.call("wait", convert: method(:duration))
    when "rules.list", "replicas.list"
      nil
    else
      raise "no Ruby mapping for #{operation}"
    end
    [group, method_name, positional, keywords]
  end

  def invoke(client, operation, args)
    group, method_name, positional, keywords = operation_call(operation, args)
    client.public_send(group).public_send(method_name, *positional, **keywords)
  end
end

# Starts fake daemons for a test and checks, as they close, that every call
# sent this SDK's version header.
module ContractDaemons
  def setup
    super
    @daemons = []
  end

  def teardown
    @daemons.each do |daemon|
      assert_empty(daemon.sdk_versions.uniq - ["ruby/#{Litebackup::VERSION}"])
      daemon.close
    end
    super
  end

  def start_daemon(client_version: "1.1.0", &reply)
    daemon = ContractDaemon.new(client_version: client_version, &reply)
    @daemons << daemon
    daemon
  end

  def client_to(daemon)
    Litebackup::Client.new(socket_path: daemon.socket_path)
  end
end

class RequestsContractTest < Minitest::Test
  include WorkingDirectory
  include ContractDaemons

  ContractFixtures.names("requests").each do |name|
    define_method("test_#{name.tr("-", "_")}") do
      fixture = ContractFixtures.read("requests", name, @working_directory)
      daemon = start_daemon { [200, "application/json", JSON.generate(fixture.fetch("response", {}))] }
      client = client_to(daemon)
      begin
        client.connect
        if fixture.key?("error")
          raised = assert_raises(Litebackup::Error) { ContractFixtures.invoke(client, fixture["operation"], fixture["args"]) }
          assert_equal(fixture["error"]["code"], raised.code)
          assert_empty(daemon.requests)
          next
        end
        ContractFixtures.invoke(client, fixture["operation"], fixture["args"])
        assert_equal(1, daemon.requests.length)
        sent = daemon.requests.first
        expected = fixture["request"]
        assert_equal([expected["method"], expected["path"], expected["query"]],
                     [sent.http_method, sent.path, sent.query])
        if expected["body"].nil?
          assert_nil(sent.json)
        else
          assert_equal(expected["body"], sent.json)
        end
      ensure
        client.close
      end
    end
  end
end

class ErrorsContractTest < Minitest::Test
  include ContractDaemons

  ContractFixtures.names("errors").each do |name|
    define_method("test_#{name.tr("-", "_")}") do
      fixture = ContractFixtures.read("errors", name)
      response = fixture["response"]
      daemon = start_daemon { [response["status"], response["content_type"], response["body"]] }
      client = client_to(daemon)
      begin
        error = assert_raises(Litebackup::Error) { client.rules.list }
        expected = fixture["expect"]
        assert_equal([expected["code"], expected["message"], expected["status"], expected["details"]],
                     [error.code, error.message, error.status, error.details])
        assert_equal("#{expected["code"]}: #{expected["message"]}", error.detailed_message)
      ensure
        client.close
      end
    end
  end
end

class VersionContractTest < Minitest::Test
  include ContractDaemons

  ContractFixtures.names("version").each do |name|
    define_method("test_#{name.tr("-", "_")}") do
      fixture = ContractFixtures.read("version", name)
      daemon = start_daemon(client_version: fixture["client_version"]) { [500, "text/plain", ""] }
      client = client_to(daemon)
      begin
        if fixture["accepted"]
          client.connect
          next
        end
        error = assert_raises(Litebackup::Error) { client.connect }
        assert_equal(fixture["expect"]["code"], error.code)
        assert_includes(error.message, Litebackup::MINIMUM_DAEMON_VERSION)
        assert_includes(error.message, "litebackup drain")
      ensure
        client.close
      end
    end
  end
end

class PaginationContractTest < Minitest::Test
  include ContractDaemons

  def exchange_daemon(exchanges)
    position = exchanges.each
    start_daemon do |received|
      exchange = begin
        position.next
      rescue StopIteration
        nil
      end
      next [500, "application/json", '{"code":"unexpected","message":"no more exchanges"}'] if exchange.nil?

      unless { "method" => received.http_method, "path" => received.path,
               "query" => received.query } == exchange["request"]
        next [500, "application/json", JSON.generate("code" => "unexpected", "message" => received.inspect)]
      end

      [exchange["response"]["status"], "application/json", JSON.generate(exchange["response"]["body"])]
    end
  end

  def iteration(client, fixture)
    args = fixture["args"]
    case fixture["operation"]
    when "databases.all" then client.databases.all
    when "databases.versions" then client.databases.all_versions(args["name"], after: args.fetch("after", 0),
                                                                               limit: args["limit"])
    when "events.watch" then client.events.watch(after: args.fetch("after", 0),
                                                 wait: ContractFixtures.duration(args["wait"]))
    else raise "no Ruby mapping for #{fixture["operation"]}"
    end
  end

  # An iterated field as a fixture writes it: times as RFC 3339 in UTC.
  def fixture_value(value)
    value.is_a?(Time) ? value.utc.iso8601 : value
  end

  %w[pagination events].each do |fixture_directory|
    ContractFixtures.names(fixture_directory).each do |name|
      define_method("test_#{fixture_directory}_#{name.tr("-", "_")}") do
        fixture = ContractFixtures.read(fixture_directory, name)
        skip(fixture["skip"]["ruby"]) if fixture.fetch("skip", {}).key?("ruby")
        daemon = exchange_daemon(fixture["exchanges"])
        client = client_to(daemon)
        item_limit = fixture["item_limit"]
        key = fixture["item_key"]
        items = []
        ended = nil
        begin
          begin
            iteration(client, fixture).each do |item|
              items << fixture_value(item.public_send(key))
              break if !item_limit.nil? && items.length >= item_limit
            end
          rescue Litebackup::Error => e
            ended = e
          end
          assert_equal(fixture["expect"]["items"], items)
          if fixture["expect"].key?("error")
            refute_nil(ended)
            assert_equal(fixture["expect"]["error"]["code"], ended.code)
          elsif !ended.nil?
            raise ended
          end
          assert_equal(fixture["exchanges"].length, daemon.requests.length)
        ensure
          client.close
        end
      end
    end
  end
end

# Saves its steps and fails open or close on demand.
class StepAdapter
  include Litebackup::Adapter

  attr_reader :open_path, :open_error, :close_error

  def initialize(steps, failing)
    @steps = steps
    @failing = failing
    @open_path = nil
    @open_error = RuntimeError.new("open failed")
    @close_error = RuntimeError.new("close failed")
  end

  def open(path)
    @steps << "open"
    @open_path = path
    raise @open_error if @failing.include?("open")

    "connection"
  end

  def close(_handle)
    @steps << "close"
    raise @close_error if @failing.include?("close")
  end
end

class LifecycleContractTest < Minitest::Test
  include WorkingDirectory
  include ContractDaemons

  # Saves each database request as a step.
  def lifecycle_daemon(fixture, steps, failing)
    name = fixture["lease"]["name"]
    start_daemon do |received|
      step = received.path.delete_prefix("/v1/databases/")
      steps << step
      if failing.include?(step)
        next [503, "application/json", JSON.generate("code" => "unavailable", "message" => "#{step} failed")]
      end

      body = case step
             when "acquire" then fixture["acquire_response"]
             when "sync"
               { "name" => name, "path" => "/x", "role" => "origin", "state" => "synced", "acquired" => true,
                 "synced_server" => { "version" => 3 }, "synced_cloud" => { "version" => 3 } }
             else
               { "database" => { "name" => name, "path" => "/x", "role" => "origin", "state" => "synced" },
                 "checkpointed" => false, "vacuumed" => false, "offloaded" => false }
             end
      [200, "application/json", JSON.generate(body)]
    end
  end

  def flatten(error)
    return [] if error.nil?
    return error.errors if error.is_a?(Litebackup::ErrorGroup)

    [error]
  end

  # Compares what a call raised with a fixture's list, as the other runners do.
  def check_raised(call, raised, expected_errors, sources)
    errors = flatten(raised)
    assert_equal(expected_errors.length, errors.length, "#{call} raised #{errors.inspect}")
    errors.zip(expected_errors).each do |actual, expected|
      case expected["source"]
      when "work" then assert_same(sources[:work_error], actual, call)
      when "open" then assert_same(sources[:adapter].open_error, actual, call)
      else
        assert(actual.is_a?(Litebackup::Error), actual.inspect)
        assert_equal(expected["code"], actual.code, actual.inspect)
        assert_equal(expected["details"], actual.details) if expected.key?("details")
        if expected.key?("step")
          assert(actual.is_a?(Litebackup::LeaseCleanupError), actual.inspect)
          assert_equal(expected["step"], actual.step)
        end
        assert_same(sources[:adapter].close_error, actual.cause) if expected["cause"] == "close"
        if expected["cause"] == "release"
          assert(actual.cause.is_a?(Litebackup::Error), actual.cause.inspect)
          assert_equal("unavailable", actual.cause.code)
        end
      end
    end
  end

  # The release gets what closing left of the cleanup timeout, so a
  # timeout_millis up to a second short of the fixture's full timeout reads
  # as the fixture's value.
  def with_spent_cleanup(body, expected)
    return body unless body.is_a?(Hash) && expected.is_a?(Hash)

    sent = body["timeout_millis"]
    wanted = expected["timeout_millis"]
    if sent.is_a?(Integer) && wanted.is_a?(Integer) && (wanted - sent).between?(0, 1000)
      return body.merge("timeout_millis" => wanted)
    end

    body
  end

  ContractFixtures.names("lifecycle").each do |name|
    define_method("test_#{name.tr("-", "_")}") do
      fixture = ContractFixtures.read("lifecycle", name, @working_directory)
      skip(fixture["skip"]["ruby"]) if fixture.fetch("skip", {}).key?("ruby")
      skip("Ruby has no async client, so it has no cancellation; the async SDKs cover it") if fixture.key?("cancel")
      lease_lifecycle(fixture)
    end
  end

  def lease_lifecycle(fixture)
    failing = fixture["fail"]
    steps = []
    lease = fixture["lease"]
    daemon = lifecycle_daemon(fixture, steps, failing)
    client = client_to(daemon)
    adapter = StepAdapter.new(steps, failing)
    sources = { work_error: nil, adapter: adapter }
    raised = nil
    close_raised = nil
    begin
      pool = client.databases.pool(adapter, cleanup_timeout: ContractFixtures.duration(lease["cleanup_timeout"]))
    rescue StandardError => e
      raised = e
    end
    unless pool.nil?
      begin
        pool.lease(lease["name"], durability: lease["durability"], path: lease["path"],
                                  offload: lease.fetch("offload", false),
                                  require_existing: lease.fetch("require_existing", false)) do
          steps << "work"
          if failing.include?("work")
            sources[:work_error] = RuntimeError.new("work failed")
            raise sources[:work_error]
          end
        end
      rescue StandardError => e
        raised = e
      end
      begin
        pool.close
      rescue StandardError => e
        close_raised = e
      end
    end
    client.close
    expect = fixture["expect"]
    assert_equal(expect["steps"], steps)
    assert_equal(expect["open_path"], adapter.open_path) if expect["open_path"]
    assert_path_exists(expect["created_file"]) if expect["created_file"]
    refute_path_exists(expect["missing_file"]) if expect["missing_file"]
    daemon.requests.each do |received|
      expected_body = case received.path
                      when "/v1/databases/acquire" then expect["acquire_body"]
                      when "/v1/databases/release" then expect["release_body"]
                      end
      next if expected_body.nil?

      assert_equal(expected_body, with_spent_cleanup(received.json, expected_body), received.path)
    end
    check_raised("the lease", raised, expect["raised"], sources)
    check_raised("closing the pool", close_raised, expect["close_raised"], sources)
  end
end

class CredentialsContractTest < Minitest::Test
  ContractFixtures.names("credentials").each do |name|
    define_method("test_#{name.tr("-", "_")}") do
      fixture = ContractFixtures.read("credentials", name)
      expect = fixture["expect"]
      if expect
        error = assert_raises(Litebackup::Error) { Litebackup::Credential.credential_type(fixture["api_key"]) }
        assert_equal(expect["code"], error.code)
        secret = fixture["api_key"].to_s.strip.split("@", 2).first.to_s
        refute_includes(error.message, secret) unless secret.empty?
        next
      end
      expected_type = fixture["type"]
      if expected_type.nil?
        assert_nil(Litebackup::Credential.credential_type(fixture["api_key"]))
      else
        assert_equal(expected_type, Litebackup::Credential.credential_type(fixture["api_key"]))
      end
    end
  end
end

class BinaryContractTest < Minitest::Test
  include WorkingDirectory

  FIXTURE = JSON.parse(File.read(File.join(CONTRACT_ROOT, "binary", "override-missing.json")))

  def environment
    { "PATH" => ENV.fetch("PATH", ""), "HOME" => @working_directory,
      "XDG_RUNTIME_DIR" => File.join(@working_directory, "run") }
  end

  FIXTURE["overrides"].each do |override|
    define_method("test_missing_override_#{override}") do
      missing = File.join(@working_directory, FIXTURE["missing_file"])
      search = environment
      option = nil
      case override
      when "option" then option = missing
      when "environment" then search["LITEBACKUP_PATH"] = missing
      else flunk("unknown override #{override}")
      end
      error = assert_raises(Litebackup::Error) { Litebackup::Binary.litebackup_binary(search, option) }
      assert_equal(FIXTURE["code"], error.code)
      assert_includes(error.message, FIXTURE["message_contains"])
      assert_includes(error.message, missing)
    end
  end

  def test_override_that_exists
    unless Process.euid.zero?
      denied = File.join(@working_directory, FIXTURE["denied_file"])
      FileUtils.mkdir_p(File.dirname(denied))
      File.write(denied, "#!/bin/sh\n")
      File.chmod(0o755, denied)
      File.chmod(0, File.dirname(denied))
      begin
        assert_equal(denied, Litebackup::Binary.litebackup_binary(environment, denied))
      ensure
        File.chmod(0o755, File.dirname(denied))
      end
    end
    script = File.join(@working_directory, "litebackup")
    File.write(script, FIXTURE["interpreter_script"])
    File.chmod(0o755, script)
    Litebackup::Client.open(session: "jobs", binary_path: script, environment: environment) do |client|
      error = assert_raises(Litebackup::Error) { client.backups.once(File.join(@working_directory, "a.db")) }
      assert_equal(FIXTURE["interpreter_code"], error.code)
      assert_includes(error.message, FIXTURE["interpreter_message_contains"])
    end
  end

  def test_override_under_a_file
    under_file = File.join(@working_directory, FIXTURE["not_directory_file"])
    File.write(File.dirname(under_file), "a file, not a directory")
    Litebackup::Client.open(session: "jobs", binary_path: under_file, environment: environment) do |client|
      error = assert_raises(Litebackup::Error) { client.backups.once(File.join(@working_directory, "a.db")) }
      assert_equal(FIXTURE["code"], error.code)
    end
    assert_equal(FIXTURE["code"], Litebackup::Foreground.start_failure(under_file, Errno::ENOTDIR.new).code)
    existing = File.join(@working_directory, "litebackup")
    File.write(existing, "#!/bin/sh\n")
    assert_equal(FIXTURE["interpreter_code"], Litebackup::Foreground.start_failure(existing, Errno::ENOTDIR.new).code)
  end
end

class ModelContractTest < Minitest::Test
  # The databases-get answer carries the server time as synced_server.time
  # and the archive time as synced_cloud.archived.
  def test_status_times
    status = Litebackup::DatabaseStatus.from_wire(ContractFixtures.read("requests", "databases-get")["response"])
    assert_equal(Time.iso8601("2026-11-15T09:31:00Z"), status.synced_server.time)
    assert_equal(Time.iso8601("2026-11-15T09:30:00Z"), status.synced_cloud.archived)
  end

  # An acquired database's answer carries offload_durability, and one not acquired has none.
  def test_status_offload_durability
    acquired = Litebackup::AcquireResult.from_wire(ContractFixtures.read("requests", "acquire-all")["response"])
    assert_equal("cloud", acquired.database.offload_durability)
    status = Litebackup::DatabaseStatus.from_wire(ContractFixtures.read("requests", "databases-get")["response"])
    assert_nil(status.offload_durability)
  end

  # The shutdown-all answer carries a database the drain did not drain, with its code and message.
  def test_shutdown_failed
    shutdown = Litebackup::ShutdownResult.from_wire(ContractFixtures.read("requests", "shutdown-all")["response"])
    assert_equal(2, shutdown.released)
    assert_equal([["orders.sqlite", Litebackup::ErrorCode::LOCK_UNAVAILABLE,
                   "another machine writes the database (worker-3)"]],
                 shutdown.failed.map { |failure| [failure.name, failure.code, failure.message] })
    assert_empty(Litebackup::ShutdownResult.from_wire(ContractFixtures.read("requests",
                                                                            "shutdown-default")["response"]).failed)
  end
end
