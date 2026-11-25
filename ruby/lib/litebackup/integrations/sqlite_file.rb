# frozen_string_literal: true

require "uri"

module Litebackup
  module Integrations
    # SQLite filenames and file: URIs, resolved as sqlite3ParseUri does.
    module SQLiteFile
      NO_FILE = "an in-memory or temporary database has no file to back up; open it from a path"

      module_function

      # The absolute file a file: URI opens, against base for a relative path.
      def file_from_uri(uri, base = Dir.pwd)
        rest = uri.delete_prefix("file:")
        if rest.start_with?("//")
          authority_end = rest.index(%r{[/?#]}, 2) || rest.length
          authority = rest[2...authority_end]
          unless ["", "localhost"].include?(authority)
            raise Litebackup.invalid_argument(
              "SQLite opens only local files, and #{uri.inspect} names host #{authority.inspect}"
            )
          end

          rest = rest[authority_end..]
        end
        encoded_path, query = rest.split("#", 2).first.split("?", 2)
        pairs = query.to_s.split("&").reject(&:empty?).map do |part|
          part.split("=", 2).map do |text|
            decode(text.to_s)
          end
        end
        raise Litebackup.invalid_argument(NO_FILE) if pairs.include?(%w[mode memory]) || pairs.include?(%w[vfs memdb])

        plain_file(decode(encoded_path.to_s), base)
      end

      # SQLite URI percent-decoding; unlike a form, + stays +.
      def decode(text)
        URI.decode_uri_component(text)
      end

      # The absolute file a plain SQLite filename opens; "" and ":memory:" open none.
      def plain_file(filename, base = Dir.pwd)
        raise Litebackup.invalid_argument(NO_FILE) if ["", ":memory:"].include?(filename)

        Wire.absolute_path(filename.start_with?("/") ? filename : File.join(base, filename))
      end

      # The file of a SQLite filename or file: URI.
      def database_file(filename, base = Dir.pwd)
        filename.start_with?("file:") ? file_from_uri(filename, base) : plain_file(filename, base)
      end

      # The file of one schema in PRAGMA database_list entries of seq, name, and file.
      def file_from_database_list(entries, schema)
        entries.each do |entry|
          seq_name_file = entry.is_a?(Hash) ? [entry["seq"], entry["name"], entry["file"]] : entry
          next unless seq_name_file[1] == schema

          file = seq_name_file[2]
          unless file.is_a?(String) && !file.empty?
            raise Litebackup.invalid_argument(
              "the database #{schema.inspect} is in memory or temporary and has no file to back up"
            )
          end

          return file
        end
        raise Litebackup.invalid_argument("no attached database named #{schema.inspect}")
      end

      # Raises not_found unless something is at path: a pool's adapter opens existing databases only.
      def require_existing_file(path)
        return if File.exist?(path)

        raise Error.new(ErrorCode::NOT_FOUND, "#{path} does not exist; the adapter opens existing databases only")
      end
    end
  end
end
