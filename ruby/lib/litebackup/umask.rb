# frozen_string_literal: true

module Litebackup
  # The caller's file mode creation mask, which acquire and restore send.
  module Umask
    # Keeps two threads of this gem from setting the umask at once.
    LOCK = Mutex.new

    module_function

    # This process's umask, so a file the daemon creates gets the bits a file
    # this process created would. On Linux it reads the Umask line of
    # /proc/self/status, which leaves the mask alone; elsewhere, as on macOS,
    # it sets the mask and puts it back at once, so a file another thread
    # creates in that moment could get the mask 0.
    def current_umask
      mask = status_umask("/proc/self/status")
      return mask unless mask.nil?

      LOCK.synchronize do
        previous = File.umask(0)
        File.umask(previous)
        previous
      end
    end

    # Reads the octal Umask line of a /proc status file, nil when it has none.
    def status_umask(path)
      # Read as bytes: the process name on the Name line need not be UTF-8.
      lines = File.binread(path).lines
      line = lines.find { |candidate| candidate.start_with?("Umask:") }
      return nil if line.nil?

      text = line.delete_prefix("Umask:").strip
      return nil unless text.match?(/\A[0-7]+\z/)

      mask = text.to_i(8)
      mask.between?(0, 0o777) ? mask : nil
    rescue SystemCallError, IOError
      nil
    end

    # The umask a request sends: the one passed in, checked, or this process's.
    def request_umask(umask)
      return current_umask if umask.nil?
      unless umask.is_a?(Integer) && umask.between?(0, 0o777)
        raise Litebackup.invalid_argument("umask must be between 0 and 0777")
      end

      umask
    end
  end
end
