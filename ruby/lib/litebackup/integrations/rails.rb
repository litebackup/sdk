# frozen_string_literal: true

require "litebackup"
require "rails/railtie"
require_relative "sqlite_file"

module Litebackup
  module Integrations
    # Registers continuous backups of a Rails app's SQLite databases when the
    # app boots. Require it from config/application.rb, after Rails, and
    # name the databases to back up:
    #
    #   require "litebackup/integrations/rails"
    #
    #   config.litebackup.databases = { primary: {}, queue: { name: "app/queue.sqlite" } }
    #
    # Each name is a database of the environment's config/database.yml,
    # the Solid Queue, Cache, and Cable databases included; its options are
    # backups.start's: name, prefix, maximum_local_databases,
    # offload_durability, and offload_rollback_journal. The client takes
    # api_key, session, socket_path, and binary_path from config.litebackup
    # too. enabled is false in the test environment and in Rake tasks
    # (assets:precompile, db:migrate), and true otherwise: the server, the
    # console, and bin/jobs.
    #
    # Before the first migration, when a database file does not exist yet,
    # it is created empty, the database SQLite would create on first
    # connect, when its directory exists. Registration runs once each time
    # the app initializes: once in Puma's master when it preloads the app
    # before forking its workers, in each worker when it does not, and once
    # in bin/jobs. backups.start returns the saved rule when the same
    # settings were registered before, so every such boot registers again
    # safely; changed settings raise rule_exists at boot.
    # The integration never shuts the daemon down.
    class Railtie < ::Rails::Railtie
      # A config.litebackup setting that cannot be registered; boot stops with it.
      class ConfigurationError < StandardError; end

      CLIENT_KEYS = %i[api_key session socket_path binary_path].freeze
      START_KEYS = %i[name prefix maximum_local_databases offload_durability offload_rollback_journal].freeze
      SETTING_KEYS = (%i[databases enabled] + CLIENT_KEYS).freeze

      config.litebackup = ActiveSupport::OrderedOptions.new
      config.litebackup.databases = {}

      config.after_initialize do |app|
        Railtie.register_backups(app.config.litebackup, ::Rails.env, ::Rails.root)
      end

      class << self
        # Whether boot registers backups: enabled when set, otherwise false in
        # the test environment and during Rake tasks.
        def enabled?(settings, environment)
          return settings[:enabled] == true unless settings[:enabled].nil?
          return false if environment == "test"

          !(defined?(::Rake.application) && ::Rake.application.top_level_tasks.any?)
        end

        # Starts the backup of each named database with a client that closes once they are saved.
        def register_backups(settings, environment, root)
          return unless enabled?(settings, environment)

          databases = settings[:databases] || {}
          return if databases.empty?

          unknown = settings.keys.map(&:to_sym) - SETTING_KEYS
          unless unknown.empty?
            raise ConfigurationError,
                  "config.litebackup has unknown keys: #{unknown.sort.join(", ")}"
          end

          registrations = databases.map do |database_name, options|
            [database_file(database_name.to_s, environment, root), start_options(database_name, options)]
          end
          client_options = CLIENT_KEYS.to_h { |key| [key, settings[key]] }.compact
          Litebackup::Client.open(**client_options) do |client|
            registrations.each do |path, options|
              client.backups.start(path, **options)
            rescue Litebackup::Error => e
              raise ConfigurationError, "registering the backup of #{path} failed: #{e.code}: #{e.message}"
            end
          end
        rescue Litebackup::Error => e
          raise ConfigurationError, "config.litebackup: #{e.code}: #{e.message}"
        end

        def start_options(database_name, options)
          options = (options || {}).to_h.transform_keys(&:to_sym)
          extra = options.keys - START_KEYS
          unless extra.empty?
            raise ConfigurationError,
                  "config.litebackup.databases: #{database_name} has unknown keys: #{extra.sort.join(", ")}"
          end

          options
        end

        # The file a configured SQLite database opens, created empty when it
        # does not exist yet; refuses other adapters and in-memory and test databases.
        def database_file(database_name, environment, root)
          database_config = ::ActiveRecord::Base.configurations.configs_for(env_name: environment, name: database_name,
                                                                            include_hidden: true)
          if database_config.nil?
            raise ConfigurationError,
                  "config.litebackup.databases: #{database_name} is not a database of the #{environment} environment"
          end
          unless database_config.adapter == "sqlite3"
            raise ConfigurationError, "config.litebackup.databases: #{database_name} uses the " \
                                      "#{database_config.adapter} adapter; only SQLite files are backed up"
          end
          if environment == "test"
            raise ConfigurationError,
                  "config.litebackup.databases: #{database_name} is a test database, which is not backed up"
          end

          path = begin
            SQLiteFile.database_file(database_config.database.to_s, root.to_s)
          rescue Litebackup::Error => e
            raise ConfigurationError, "config.litebackup.databases: #{database_name}: #{e.message}"
          end
          create_empty_database(database_name, path)
          path
        end

        # Before the first migration there is no file yet. An empty file is
        # the empty database SQLite would create on first connect, and the
        # daemon backs it up from its first write.
        def create_empty_database(database_name, path)
          unless File.directory?(File.dirname(path))
            raise ConfigurationError,
                  "config.litebackup.databases: #{database_name}: the directory of #{path} does not exist"
          end

          File.open(path, File::CREAT | File::EXCL | File::WRONLY, 0o644, &:close)
        rescue Errno::EEXIST
          nil
        end
      end
    end
  end
end
