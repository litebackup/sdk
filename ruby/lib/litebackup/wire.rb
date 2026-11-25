# frozen_string_literal: true

require "json"
require "pathname"
require "uri"

module Litebackup
  Durability = Models::Durability

  # How a replica keeps the app from writing its file.
  module WriteProtection
    LOCK = Models::ReplicaRequest::WRITE_PROTECTION_LOCK
    DETECT = Models::ReplicaRequest::WRITE_PROTECTION_DETECT
    NONE = Models::ReplicaRequest::WRITE_PROTECTION_NONE
    ALL = [LOCK, DETECT, NONE].freeze
  end

  # Request building and answer decoding, apart from the transport: every
  # operation is a method returning a Call, the request to send and how to
  # decode its answer.
  module Wire
    SDK_VERSION_HEADER = "Litebackup-SDK-Version"
    CLIENT_VERSION_HEADER = "litebackup-client-version"
    SDK_VERSION_VALUE = "ruby/#{VERSION}".freeze
    # Added to a call's server-side timeout for its client-side deadline, so
    # the daemon's own timeout answer arrives first.
    DEADLINE_GRACE_SECONDS = 30.0
    EVENT_WAIT_SECONDS = 30.0
    FIRST_RETRY_SECONDS = 0.25
    MAXIMUM_RETRY_SECONDS = 10.0
    # Limits one answer.
    RESPONSE_LIMIT = 64 << 20

    ROLE_ORIGIN = Models::DatabaseStatus::ROLE_ORIGIN
    STATE_OFFLOADED = Models::DatabaseStatus::STATE_OFFLOADED
    STATE_CONFLICTED = Models::DatabaseStatus::STATE_CONFLICTED
    JOURNAL_MODE_ROLLBACK = "rollback"

    # One request and how to decode its answer. timeout_seconds is the
    # server-side timeout, which the client waits for plus
    # DEADLINE_GRACE_SECONDS; answer_deadline_seconds replaces that deadline,
    # as a failover's role confirmation limit does.
    Call = Data.define(:http_method, :path, :query, :body, :timeout_seconds, :decoder, :answer_deadline_seconds) do
      def self.get(path, query = {}, decoder:, timeout_seconds: nil)
        new(http_method: "GET", path: path, query: query, body: nil, timeout_seconds: timeout_seconds,
            decoder: decoder, answer_deadline_seconds: nil)
      end

      def self.post(path, body, decoder:, timeout_seconds: nil)
        new(http_method: "POST", path: path, query: {}, body: body, timeout_seconds: timeout_seconds,
            decoder: decoder, answer_deadline_seconds: nil)
      end

      def target
        query.empty? ? path : "#{path}?#{URI.encode_www_form(query)}"
      end

      def deadline_seconds
        return answer_deadline_seconds unless answer_deadline_seconds.nil?

        timeout_seconds.nil? ? nil : timeout_seconds + DEADLINE_GRACE_SECONDS
      end

      def encoded_body
        body.nil? ? nil : JSON.generate(body)
      end
    end

    module_function

    # A decoder into a generated model.
    def model(model_class)
      ->(value) { model_class.from_wire(value) }
    end

    NOTHING = ->(_value) {}

    # Decodes an answer, raising Error for anything but 200.
    def decode_answer(call, status, body)
      raise response_error(status, body) unless status == 200

      begin
        call.decoder.call(JSON.parse(body.dup.force_encoding(Encoding::UTF_8)))
      rescue JSON::ParserError, TypeError, KeyError, ArgumentError, NoMethodError => e
        raise Error.new(ErrorCode::INVALID_RESPONSE, "decoding #{call.path}: #{e.message}", status)
      end
    end

    # A non-200 answer: a JSON object's code, message, and remaining fields,
    # or http_<status> with the text.
    def response_error(status, body)
      text = body.dup.force_encoding(Encoding::UTF_8).scrub
      fallback = Error.new("http_#{status}", text.strip, status)
      begin
        decoded = JSON.parse(text)
      rescue JSON::ParserError
        return fallback
      end
      return fallback unless decoded.is_a?(Hash)

      code = decoded.delete("code")
      message = decoded.delete("message")
      unless code.is_a?(String) && !code.empty?
        return message.is_a?(String) ? Error.new("http_#{status}", message, status) : fallback
      end

      Error.new(code, message.is_a?(String) ? message : "", status, decoded)
    end

    # Orders dotted numeric versions, ignoring any -suffix; parts that are not
    # numbers count as zero.
    def compare_versions(left, right)
      parts = lambda { |version|
        version.split("-", 2).first.to_s.split(".").map do |part|
          part.match?(/\A\d+\z/) ? part.to_i : 0
        end
      }
      left_parts = parts.call(left)
      right_parts = parts.call(right)
      length = [left_parts.length, right_parts.length].max
      left_parts.fill(0, left_parts.length...length) <=> right_parts.fill(0, right_parts.length...length)
    end

    # Refuses a daemon older than MINIMUM_DAEMON_VERSION, saying how to replace it.
    def check_daemon_version(client_version, session)
      return if !client_version.empty? && compare_versions(client_version, MINIMUM_DAEMON_VERSION) >= 0

      reported = client_version.empty? ? "does not report its version" : "is version #{client_version}"
      drain = session ? "litebackup drain --session #{session}" : "litebackup drain"
      raise Error.new(
        ErrorCode::DAEMON_UNSUPPORTED,
        "the litebackup daemon #{reported}, and this SDK needs #{MINIMUM_DAEMON_VERSION} or later. " \
        "Stop it with `#{drain}` so the next call starts the new binary, " \
        "or restart the service if `litebackup install service` runs it"
      )
    end

    # The connect probe every SDK sends.
    def connect_probe
      Call.get("/v1/status/show", { "limit" => "1" }, decoder: model(Models::Status))
    end

    # Whether an optional argument holds something: not nil and not "".
    def supplied?(value)
      !value.nil? && value != ""
    end

    # A path as text: a String, or anything with to_path, such as a Pathname.
    def path_text(source)
      return source if source.is_a?(String)
      return source.to_path if source.respond_to?(:to_path)

      raise Litebackup.invalid_argument("expected a path, got #{source.class}")
    end

    # Resolves a path against the working directory and cleans it lexically;
    # the daemon needs absolute paths. A ~ is a name like any other.
    def absolute_path(source)
      path = path_text(source)
      path = File.join(Dir.pwd, path) unless path.start_with?("/")
      Pathname.new(path).cleanpath.to_s
    end

    # A duration in seconds, or nil when unset or zero, so the daemon's
    # default applies. Durations are seconds as a Numeric, such as 90 or 1.5,
    # or an ActiveSupport::Duration.
    def seconds(duration, name)
      return nil if duration.nil?
      unless duration.is_a?(Numeric)
        raise Litebackup.invalid_argument("#{name} must be a number of seconds, got #{duration.class}")
      end

      duration_seconds = duration.to_f
      if duration_seconds.negative? || duration_seconds.nan?
        raise Litebackup.invalid_argument("#{name} must not be negative")
      end
      raise Litebackup.invalid_argument("#{name} must be finite") if duration_seconds.infinite?

      duration_seconds.zero? ? nil : duration_seconds
    end

    def millis(duration_seconds)
      duration_seconds.nil? ? nil : (duration_seconds * 1000).round
    end

    # UTC with Z, fractional seconds only when they are not zero.
    def wire_time(time)
      time = parse_time(time) if time.is_a?(String)
      time = time.to_time if !time.is_a?(Time) && time.respond_to?(:to_time)
      raise Litebackup.invalid_argument("expected a Time, got #{time.class}") unless time.is_a?(Time)

      utc = time.getutc
      text = utc.strftime("%Y-%m-%dT%H:%M:%S")
      text += ".#{format("%09d", utc.nsec).sub(/0+\z/, "")}" unless utc.nsec.zero?
      "#{text}Z"
    end

    def parse_time(text)
      Time.iso8601(text)
    rescue ArgumentError
      raise Litebackup.invalid_argument("expected an ISO 8601 time with its zone, got #{text.inspect}")
    end

    # A request body without its unset fields: nil, false, and "" are left out.
    def compact_body(fields)
      fields.reject { |_key, value| value.nil? || value == false || value == "" }
    end

    # A page limit the daemon accepts: a whole number from 1 to 1000.
    def page_limit(limit)
      unless limit.is_a?(Integer) && limit.between?(1, 1000)
        raise Litebackup.invalid_argument("the limit must be a whole number from 1 to 1000")
      end

      limit.to_s
    end

    def status(after: "", limit: 100)
      Call.get("/v1/status/show", { "after" => after.to_s, "limit" => page_limit(limit) }, decoder: model(Models::Status))
    end

    def databases_page(after: "", limit: 100)
      Call.get("/v1/databases/list", { "after" => after.to_s, "limit" => page_limit(limit) },
               decoder: model(Models::DatabaseStatusList))
    end

    def database(name)
      Call.get("/v1/databases/status", { "name" => name }, decoder: model(Models::DatabaseStatus))
    end

    def sync(name)
      Call.post("/v1/databases/sync", { "name" => name }, decoder: model(Models::DatabaseStatus))
    end

    def check_durability(durability)
      return if durability.nil? || Durability::ALL.include?(durability)

      raise Litebackup.invalid_argument("offload durability must be server or cloud")
    end

    # Checks an offload durability and returns it, or nil for the daemon's
    # default, server.
    def offload_durability_choice(durability)
      check_durability(durability)
      durability == Durability::SERVER ? nil : durability
    end

    def offload(name, durability: nil)
      check_durability(durability)
      Call.post("/v1/databases/offload", { "name" => name }.merge(compact_body("durability" => durability)),
                decoder: model(Models::DatabaseStatus))
    end

    def writer(name)
      Call.get("/v1/databases/writer", { "name" => name }, decoder: model(Models::Writer))
    end

    def versions_page(name, after: 0, limit: nil)
      query = { "name" => name }
      query["after"] = after.to_s if after.positive?
      query["limit"] = limit.to_s unless limit.nil?
      Call.get("/v1/databases/versions", query, decoder: model(Models::VersionList))
    end

    def restore(name, path:, version: nil, time: nil, umask: nil)
      raise Litebackup.invalid_argument("restore needs a path to write to") unless supplied?(path)
      raise Litebackup.invalid_argument("restore takes a version or a time, not both") if !version.nil? && !time.nil?

      body = { "name" => name, "path" => absolute_path(path) }
      body["version"] = version unless version.nil?
      body["time"] = wire_time(time) unless time.nil?
      body["umask"] = Umask.request_umask(umask)
      Call.post("/v1/databases/restore", body, decoder: model(Models::RestoreResult))
    end

    def revert(name, version: nil, time: nil)
      raise Litebackup.invalid_argument("revert takes a version or a time, one of them") if version.nil? == time.nil?

      body = { "name" => name }
      unless version.nil?
        unless version.is_a?(Integer) && version >= 1
          raise Litebackup.invalid_argument("revert needs a positive whole version")
        end

        body["version"] = version
      end
      body["time"] = wire_time(time) unless time.nil?
      Call.post("/v1/databases/revert", body, decoder: model(Models::RevertResult))
    end

    def acquire(name, path: nil, maintenance: false, offload_on_release: false, offload_durability: Durability::SERVER,
                machine_label: nil, timeout: nil, lock_timeout: nil, umask: nil, discard_local: false)
      timeout_seconds = seconds(timeout, "the acquire timeout")
      lock_timeout_seconds = seconds(lock_timeout, "the lock timeout")
      # name is required, so it is sent even when empty and the daemon answers for it.
      body = { "name" => name }.merge(compact_body(
                                        "path" => supplied?(path) ? absolute_path(path) : nil,
                                        "maintenance" => maintenance,
                                        "discard_local" => discard_local,
                                        "offload_on_release" => offload_on_release,
                                        "offload_durability" => offload_durability_choice(offload_durability),
                                        "machine_label" => machine_label,
                                        "timeout_millis" => millis(timeout_seconds),
                                        "lock_timeout_millis" => millis(lock_timeout_seconds)
                                      ))
      body["umask"] = Umask.request_umask(umask)
      # No timeout leaves the call without a deadline, as the daemon applies its own.
      call_seconds = timeout_seconds.nil? ? nil : timeout_seconds + (lock_timeout_seconds || 0)
      Call.post("/v1/databases/acquire", body, timeout_seconds: call_seconds, decoder: model(Models::AcquireResult))
    end

    def release(name, durability: nil, offload: false, timeout: nil)
      timeout_seconds = seconds(timeout, "the release timeout")
      body = { "name" => name }.merge(compact_body(
                                        "durability" => durability, "offload" => offload,
                                        "timeout_millis" => millis(timeout_seconds)
                                      ))
      Call.post("/v1/databases/release", body, timeout_seconds: timeout_seconds, decoder: model(Models::ReleaseResult))
    end

    def start_backup(source, name: nil, prefix: nil, maximum_local_databases: nil,
                     offload_durability: Durability::SERVER, offload_rollback_journal: false)
      raise Litebackup.invalid_argument("backup needs a path") unless supplied?(source)
      raise Litebackup.invalid_argument("choose a name or a prefix") if supplied?(name) && supplied?(prefix)

      body = compact_body(
        "path" => absolute_path(source),
        "name" => name,
        "prefix" => prefix,
        "maximum_local_databases" => maximum_local_databases == 0 ? nil : maximum_local_databases,
        "offload_durability" => offload_durability_choice(offload_durability),
        "offload_rollback_journal" => offload_rollback_journal
      )
      Call.post("/v1/backup-rules/start", body, decoder: model(Models::BackupRule))
    end

    def stop_backup(name)
      raise Litebackup.invalid_argument("stopping a backup needs a name") unless supplied?(name)

      Call.post("/v1/backup-rules/stop", { "name" => name }, decoder: model(Models::StopBackupResult))
    end

    def stop_rule(rule_id)
      raise Litebackup.invalid_argument("stopping a rule needs its ID") unless supplied?(rule_id)

      Call.post("/v1/backup-rules/stop", { "rule_id" => rule_id }, decoder: model(Models::StopBackupResult))
    end

    def list_rules
      Call.get("/v1/backup-rules/list", decoder: ->(value) { Models::BackupList.from_wire(value).rules })
    end

    def start_replica(name, path: nil, write_protection: nil, timeout: nil)
      timeout_seconds = seconds(timeout, "the replica timeout")
      body = compact_body(
        "name" => name, "path" => supplied?(path) ? absolute_path(path) : nil,
        "write_protection" => write_protection, "timeout_millis" => millis(timeout_seconds)
      )
      decoder = lambda do |value|
        started = Models::ReplicaResult.from_wire(value)
        if started.database.nil?
          raise Error.new(ErrorCode::INVALID_RESPONSE, "the daemon did not return the replica", 200)
        end

        started.database
      end
      Call.post("/v1/replicas/start", body, timeout_seconds: timeout_seconds, decoder: decoder)
    end

    def start_prefix_replica(prefix, directory:, write_protection: nil)
      raise Litebackup.invalid_argument("replicating a prefix needs a directory") unless supplied?(directory)

      body = compact_body("directory" => absolute_path(directory), "write_protection" => write_protection)
      body = { "prefix" => prefix }.merge(body)
      decoder = lambda do |value|
        started = Models::ReplicaResult.from_wire(value)
        if started.prefix_replica.nil?
          raise Error.new(ErrorCode::INVALID_RESPONSE, "the daemon did not return the prefix replica", 200)
        end

        started.prefix_replica
      end
      Call.post("/v1/replicas/start", body, decoder: decoder)
    end

    def list_replicas
      Call.get("/v1/replicas/list", decoder: model(Models::ReplicaList))
    end

    def stop_replica(name)
      Call.post("/v1/replicas/stop", { "name" => name }, decoder: NOTHING)
    end

    def stop_prefix_replica(prefix, directory:)
      raise Litebackup.invalid_argument("stopping a prefix replica needs a directory") unless supplied?(directory)

      Call.post("/v1/replicas/stop", { "prefix" => prefix, "directory" => absolute_path(directory) }, decoder: NOTHING)
    end

    def events_page(after: 0, wait: nil, limit: nil)
      wait_seconds = seconds(wait, "the event wait")
      query = {}
      query["after"] = after.to_s unless after.zero?
      query["wait_millis"] = millis(wait_seconds).to_s unless wait_seconds.nil?
      query["limit"] = limit.to_s unless limit.nil? || limit.zero?
      Call.get("/v1/events/list", query, timeout_seconds: wait_seconds, decoder: model(Models::EventList))
    end

    def shutdown(durability: nil, timeout: nil)
      timeout_seconds = seconds(timeout, "the shutdown timeout")
      body = compact_body("durability" => durability, "timeout_millis" => millis(timeout_seconds))
      Call.post("/v1/session/shutdown", body, timeout_seconds: timeout_seconds, decoder: model(Models::ShutdownResult))
    end

    def start_failover(path, name: nil, timeout: nil, discard_local: false)
      raise Litebackup.invalid_argument("failover needs a path") unless supplied?(path)

      timeout_seconds = seconds(timeout, "the failover timeout")
      body = compact_body(
        "path" => absolute_path(path), "name" => name,
        "timeout_millis" => millis(timeout_seconds), "discard_local" => discard_local
      )
      Call.post("/v1/failovers/start", body, timeout_seconds: timeout_seconds, decoder: model(Models::Failover))
    end

    # Errors that end an event watch instead of being retried.
    TERMINAL_WATCH_CODES = [
      ErrorCode::SDK_UNSUPPORTED, ErrorCode::CLOSED, ErrorCode::CANCELLED,
      ErrorCode::INVALID_ARGUMENT, ErrorCode::CURSOR_EXPIRED
    ].freeze

    def terminal_watch_error?(error)
      TERMINAL_WATCH_CODES.include?(error.code)
    end
  end
end
