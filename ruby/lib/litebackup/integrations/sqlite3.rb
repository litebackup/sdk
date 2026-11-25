# frozen_string_literal: true

require "sqlite3"
require "litebackup"
require_relative "sqlite_file"

module Litebackup
  module Integrations
    # The sqlite3 gem: the file a database has open, and a pool adapter.
    #
    #   require "litebackup/integrations/sqlite3"
    #
    #   db = SQLite3::Database.new("data/app.sqlite")
    #   litebackup.backups.start(Litebackup::Integrations::SQLite3.database_path(db))
    module SQLite3
      # Opens existing files with SQLite3::Database.new(path, flags: READWRITE,
      # **options), which never creates one; a missing file raises
      # Litebackup::Error with code not_found. Closing rolls back a
      # transaction the block left open, never commits it, and closes the
      # database. Commit what the block should keep.
      class Adapter
        include Litebackup::Adapter

        def initialize(options)
          @options = options.dup.freeze
        end

        def open(path)
          SQLiteFile.require_existing_file(path)
          ::SQLite3::Database.new(path, **@options, flags: ::SQLite3::Constants::Open::READWRITE)
        end

        def close(handle)
          handle.rollback if handle.transaction_active?
        ensure
          handle.close
        end
      end

      module_function

      # The absolute path of the file a database has open, or of an attached
      # database. Reads PRAGMA database_list, which starts no transaction.
      # Raises invalid_argument for in-memory and temporary databases.
      def database_path(database, schema = "main")
        SQLiteFile.file_from_database_list(database.execute("PRAGMA database_list"), schema)
      end

      # A pool adapter; options go to SQLite3::Database.new, such as results_as_hash: true.
      def adapter(**options)
        Adapter.new(options)
      end
    end
  end
end
