# frozen_string_literal: true

require_relative "test_helper"

# Boots a minimal Rails 8 app with primary, queue, cache, and cable SQLite
# databases against the fake daemon. A Rails app initializes once per
# process, so each boot runs in a forked child, which reports what boot
# raised and the database files it created.
class RailsIntegrationTest < Minitest::Test
  include WorkingDirectory

  DATABASE_YML = <<~YAML
    production:
      primary:
        adapter: sqlite3
        database: storage/production.sqlite3
      queue:
        adapter: sqlite3
        database: storage/production_queue.sqlite3
        migrations_paths: db/queue_migrate
      cache:
        adapter: sqlite3
        database: storage/production_cache.sqlite3
        migrations_paths: db/cache_migrate
      cable:
        adapter: sqlite3
        database: storage/production_cable.sqlite3
        migrations_paths: db/cable_migrate
      memory:
        adapter: sqlite3
        database: ":memory:"
      missing_directory:
        adapter: sqlite3
        database: nowhere/app.sqlite3
      postgres:
        adapter: postgresql
        database: app
    test:
      primary:
        adapter: sqlite3
        database: storage/test.sqlite3
  YAML

  def setup
    super
    FileUtils.mkdir_p(%w[config storage])
    File.write("config/database.yml", DATABASE_YML)
    @rules = []
    @starts = []
    @daemon = ContractDaemon.new do |received|
      next json_answer({}) unless received.path == "/v1/backup-rules/start"

      body = received.json
      @starts << body
      saved = @rules.find { |rule| rule["path"] == body["path"] }
      if saved && saved != body
        next json_answer({ "code" => "rule_exists", "message" => "#{body["path"]} has a rule with other settings",
                           "rule" => saved }, 409)
      end

      @rules << body unless saved
      json_answer(body.merge("id" => "rule-#{@rules.length}", "watch" => "inotify"))
    end
  end

  def teardown
    @daemon.close
    super
  end

  # Boots the app in a child with these config.litebackup settings, in
  # environment, while rake_tasks run; returns what boot raised, or nil.
  def boot(environment: "production", rake_tasks: [], **settings)
    reader, writer = IO.pipe
    pid = fork do
      reader.close
      writer.write(boot_in_child(settings, environment, rake_tasks))
      writer.close
      exit!(0)
    end
    writer.close
    report = reader.read
    Process.wait(pid)
    report.empty? ? nil : report
  end

  def boot_in_child(settings, environment, rake_tasks)
    ENV["RAILS_ENV"] = environment
    require "rails"
    require "active_record/railtie"
    require "litebackup/integrations/rails"
    unless rake_tasks.empty?
      require "rake"
      Rake.application.instance_variable_set(:@top_level_tasks, rake_tasks)
    end
    root = @working_directory
    socket_path = @daemon.socket_path
    application = Class.new(Rails::Application) do
      config.root = root
      config.eager_load = false
      config.secret_key_base = "litebackup-test-#{"0" * 48}"
      config.logger = Logger.new(nil)
      config.active_support.deprecation = :silence
      config.litebackup.socket_path = socket_path
      settings.each { |key, value| config.litebackup[key] = value }
    end
    Object.const_set(:LitebackupTestApplication, application)
    application.initialize!
    ""
  rescue Exception => e # rubocop:disable Lint/RescueException
    "#{e.class}: #{e.message}"
  end

  def test_booting_registers_named_databases_and_registers_them_again_on_the_next_boot
    databases = { primary: {}, queue: { name: "app/queue.sqlite" }, cache: {}, "cable" => {} }
    assert_nil(boot(databases: databases))
    assert_equal(%w[production production_queue production_cache production_cable].map do |file|
      File.join(@working_directory, "storage", "#{file}.sqlite3")
    end, @starts.map { |body| body["path"] })
    assert_equal("app/queue.sqlite", @starts[1]["name"])
    @starts.each { |body| assert_equal(0, File.size(body["path"])) }
    # A Puma worker or the job runner registering the same settings again is safe.
    assert_nil(boot(databases: databases))
    assert_equal(8, @starts.length)
  end

  def test_booting_with_changed_settings_raises_rule_exists
    assert_nil(boot(databases: { primary: {} }))
    report = boot(databases: { primary: { offload_durability: "cloud" } })
    assert_match(/ConfigurationError: registering the backup of .*production\.sqlite3 failed: rule_exists/, report)
  end

  def test_nothing_is_registered_without_named_databases
    assert_nil(boot)
    assert_empty(@daemon.requests)
  end

  def test_booting_with_names_and_settings_rails_does_not_know_raises
    assert_match(/unknown is not a database of the production environment/, boot(databases: { unknown: {} }))
    assert_match(/postgres uses the postgresql adapter/, boot(databases: { postgres: {} }))
    assert_match(/memory: an in-memory or temporary database/, boot(databases: { memory: {} }))
    assert_match(/the directory of .*nowhere.* does not exist/, boot(databases: { missing_directory: {} }))
    assert_match(/primary has unknown keys: path/, boot(databases: { primary: { path: "x" } }))
    assert_match(/config.litebackup has unknown keys: retention/, boot(databases: { primary: {} }, retention: 1))
    assert_empty(@daemon.requests)
  end

  def test_boot_is_disabled_in_tests_and_rake_tasks_unless_enabled
    assert_nil(boot(databases: { primary: {} }, environment: "test"))
    assert_nil(boot(databases: { primary: {} }, rake_tasks: ["assets:precompile"]))
    assert_nil(boot(databases: { primary: {} }, enabled: false))
    assert_empty(@daemon.requests)
    assert_match(/primary is a test database/, boot(databases: { primary: {} }, enabled: true, environment: "test"))
    assert_nil(boot(databases: { primary: {} }, enabled: true, rake_tasks: ["db:migrate"]))
    assert_equal(1, @starts.length)
  end
end
