# frozen_string_literal: true

module Litebackup
  # Codes the SDK raises itself, with status 0, and the daemon codes worth
  # matching. Codes the daemon answers with that are not listed here are kept
  # verbatim.
  module ErrorCode
    # An argument failed local validation; nothing was sent.
    INVALID_ARGUMENT = SpecErrorCodes::INVALID_ARGUMENT
    # No litebackup binary was found to start the daemon or a foreground backup.
    BINARY_NOT_FOUND = "binary_not_found"
    # litebackup launch ran and failed.
    LAUNCH_FAILED = SpecErrorCodes::LAUNCH_FAILED
    # litebackup launch refused api_key, a credential other than the one the
    # session's client locked its databases with; session.shutdown with the
    # current credential releases them first.
    DRAIN_REQUIRED = SpecErrorCodes::DRAIN_REQUIRED
    # The socket refused the connection or closed it mid-call.
    CONNECTION_FAILED = "connection_failed"
    # The daemon is older than MINIMUM_DAEMON_VERSION.
    DAEMON_UNSUPPORTED = "daemon_unsupported"
    # The wait was cancelled; the daemon may still finish the operation.
    CANCELLED = SpecErrorCodes::CANCELLED
    # The client was closed.
    CLOSED = "closed"
    # The daemon answered 200 with a body the SDK cannot decode.
    INVALID_RESPONSE = "invalid_response"
    # A pool could not close or release a leased database; the error is a LeaseCleanupError.
    LEASE_CLEANUP_FAILED = "lease_cleanup_failed"
    # The file a restore or an acquire would write, or a journal file beside it, already exists.
    FILE_EXISTS = SpecErrorCodes::FILE_EXISTS
    # A backup rule on this machine owns the database and its write lock, so
    # it cannot be released; stop its backup instead.
    RULE_DATABASE = SpecErrorCodes::RULE_DATABASE
    # The path already has a backup rule with different settings; details["rule"] is the saved rule.
    RULE_EXISTS = SpecErrorCodes::RULE_EXISTS
    # Another machine writes the database.
    LOCK_UNAVAILABLE = SpecErrorCodes::LOCK_UNAVAILABLE
    # This machine lost the database's write lock, as a pool's lease does when another machine broke it.
    LOCK_LOST = SpecErrorCodes::LOCK_LOST
    NOT_FOUND = SpecErrorCodes::NOT_FOUND
    # A process still has the database open, so it cannot be released.
    STILL_OPEN = SpecErrorCodes::STILL_OPEN
    # The operation's timeout ran out.
    TIMEOUT = SpecErrorCodes::TIMEOUT
    # This SDK is older than the daemon accepts.
    SDK_UNSUPPORTED = SpecErrorCodes::SDK_UNSUPPORTED
    # Another job or a continuous backup has a foreground backup's writer lock.
    BUSY = SpecErrorCodes::BUSY
    # An events cursor is older than the oldest event the daemon keeps; a
    # watch ends with it, and the caller picks a new cursor.
    CURSOR_EXPIRED = SpecErrorCodes::CURSOR_EXPIRED
  end

  # Every failure the SDK raises: the daemon's answer, or an error the SDK
  # raised itself with status 0.
  #
  # details holds the answer's fields besides code and message under their
  # wire names, such as "rule" for rule_exists from backups.start. For
  # lease_cleanup_failed it holds step, name, path, and acquired or
  # durability. The underlying error, when there is one, is cause.
  class Error < StandardError
    attr_reader :code, :status, :details

    def initialize(code, message, status = 0, details = nil)
      super(message)
      @code = code
      @status = status
      @details = details.nil? ? {} : details
    end

    # The code and the message, as an uncaught error shows them.
    def detailed_message(*)
      "#{code}: #{message}"
    end

    def inspect
      "#<#{self.class.name} code=#{code.inspect} message=#{message.inspect} " \
        "status=#{status.inspect} details=#{details.inspect}>"
    end
  end

  # The step of a lease's cleanup that failed.
  module LeaseCleanupStep
    # Closing the handle failed, so the database was not released and stays acquired by this machine.
    CLOSE = "close"
    # The release itself failed; the file is intact.
    RELEASE = "release"
  end

  # A failed release of a leased database, with code lease_cleanup_failed;
  # cause is why.
  #
  # step is "close" when closing the handle failed: the database was not
  # released and stays acquired by this machine with its file as it is; close
  # every connection, then call pool.release(name) again. It is "release"
  # when the release itself failed, as with still_open or an unreachable
  # server: the file is intact; call pool.release(name) again before another
  # machine needs it.
  class LeaseCleanupError < Error
    attr_reader :step, :name

    def initialize(step, name, message, details)
      super(ErrorCode::LEASE_CLEANUP_FAILED, message, 0, details)
      @step = step
      @name = name
    end
  end

  # Several errors raised together, the first one first, as a lease that
  # failed and then failed to clean up raises them. Ruby has no
  # ExceptionGroup, so this is the SDK's.
  class ErrorGroup < StandardError
    attr_reader :errors

    def initialize(message, errors)
      super("#{message} failed in #{errors.length} ways: #{errors.map(&:message).join("; ")}")
      @errors = errors.dup.freeze
    end
  end

  # The error for a failed local check.
  def self.invalid_argument(message)
    Error.new(ErrorCode::INVALID_ARGUMENT, message)
  end

  # Raises one error alone, or several as an ErrorGroup, first one first.
  def self.raise_together(failures, message)
    raise failures.first if failures.length == 1
    raise ErrorGroup.new(message, failures) unless failures.empty?
  end
end
