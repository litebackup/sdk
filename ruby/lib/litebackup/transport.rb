# frozen_string_literal: true

require "socket"

module Litebackup
  # HTTP/1.1 over the daemon's unix socket, with a small keep-alive pool.
  #
  # Net::HTTP times each read on its own, so a daemon that trickles an answer
  # could keep a call past its deadline; this reads with one monotonic
  # deadline for the whole call, headers and every chunk, and none when the
  # call has no timeout. One request uses a connection at a time. A GET that
  # fails on a reused connection before any answer arrives is sent once more,
  # on a new connection; nothing else is ever sent twice, and there is no
  # proxy lookup.
  class Transport
    POOL_SIZE = 4
    READ_SIZE = 65_536
    # Limits the status line and headers of one answer.
    HEADER_LIMIT = 64 << 10

    # A whole answer read from the socket.
    Answer = Data.define(:status, :client_version, :body)

    # The call's deadline passed before the daemon answered.
    class DeadlinePassed < StandardError; end

    # The daemon closed the connection, or broke HTTP, before a whole answer.
    class ConnectionBroken < StandardError; end

    # One connection and the bytes read past the last answer.
    class Connection
      attr_reader :socket
      attr_accessor :buffer

      def initialize(socket)
        @socket = socket
        @buffer = +""
      end

      # An idle connection the daemon closed reads as ready.
      def dropped?
        !socket.wait_readable(0).nil?
      rescue IOError, SystemCallError
        true
      end

      def close
        socket.close
      rescue IOError, SystemCallError
        nil
      end
    end

    attr_reader :socket_path

    def initialize(socket_path)
      @socket_path = socket_path
      @lock = Mutex.new
      @idle = []
      @active = []
      @closed = false
      @owner_pid = Process.pid
    end

    def closed?
      @closed
    end

    # Sends one request and reads its whole answer. deadline_seconds bounds
    # the whole call; nil waits as long as the daemon takes.
    def request(http_method, target, body, deadline_seconds)
      deadline = deadline_seconds.nil? ? nil : monotonic + deadline_seconds
      retried = false
      loop do
        connection, reused = lend_connection(fresh: retried)
        answer_started = false
        reusable = false
        begin
          answer, reusable = exchange(connection, reused, http_method, target, body, deadline) { answer_started = true }
          return answer
        rescue DeadlinePassed
          raise closed_error if @closed

          raise Error.new(
            ErrorCode::TIMEOUT,
            "the daemon did not answer within the call's timeout; it may still finish the operation"
          )
        rescue ConnectionBroken, IOError, SystemCallError => e
          raise closed_error if @closed

          if reused && !retried && http_method == "GET" && !answer_started
            retried = true
            next
          end
          raise Error.new(ErrorCode::CONNECTION_FAILED, e.message.empty? ? e.class.name : e.message)
        ensure
          # Every way out, a killed thread's included, gives the connection
          # back: to the idle pool only after a whole, framed answer.
          return_connection(connection, reusable)
        end
      end
    end

    # Closes idle connections and ends the calls that are waiting. In a
    # forked child it only closes the child's copies of the parent's
    # connections: shutting them down would end the parent's calls.
    def close
      idle, active = @lock.synchronize do
        @closed = true
        [@idle.slice!(0..), @active.slice!(0..)]
      end
      idle.each(&:close)
      unless Process.pid == @owner_pid
        active.each(&:close)
        return nil
      end

      active.each do |connection|
        connection.socket.shutdown(Socket::SHUT_RDWR)
      rescue IOError, SystemCallError
        nil
      end
    end

    private

    def closed_error
      Error.new(ErrorCode::CLOSED, "the client is closed")
    end

    def monotonic
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end

    # A pooled idle connection, or a new one registered before it connects,
    # so close sees it; with fresh, always a new one. A forked child drops the
    # parent's idle connections without shutting them down.
    def lend_connection(fresh:)
      @lock.synchronize do
        raise closed_error if @closed

        if Process.pid != @owner_pid
          @idle.each(&:close)
          @idle.clear
          @active.clear
          @owner_pid = Process.pid
        end
        until fresh || @idle.empty?
          connection = @idle.pop
          if connection.dropped?
            connection.close
            next
          end
          @active << connection
          return [connection, true]
        end
        connection = Connection.new(Socket.new(Socket::AF_UNIX, Socket::SOCK_STREAM))
        @active << connection
        [connection, false]
      end
    end

    def return_connection(connection, reusable)
      @lock.synchronize do
        @active.delete(connection)
        if reusable && !@closed && @idle.length < POOL_SIZE && Process.pid == @owner_pid
          @idle << connection
          return
        end
      end
      connection.close
    end

    def remaining(deadline)
      return nil if deadline.nil?

      left = deadline - monotonic
      raise DeadlinePassed, "the call's deadline passed" if left <= 0

      left
    end

    def wait(socket, direction, deadline)
      readable = direction == :read ? [socket] : nil
      writable = direction == :write ? [socket] : nil
      raise DeadlinePassed, "the call's deadline passed" if IO.select(readable, writable, nil, remaining(deadline)).nil?
    end

    def connect(socket, deadline)
      address = Socket.pack_sockaddr_un(socket_path)
      begin
        socket.connect_nonblock(address)
      rescue IO::WaitWritable
        wait(socket, :write, deadline)
        begin
          socket.connect_nonblock(address)
        rescue Errno::EISCONN
          nil
        end
      end
      raise closed_error if @closed
    end

    def request_bytes(http_method, target, body)
      lines = ["#{http_method} #{target} HTTP/1.1", "Host: litebackup", "#{Wire::SDK_VERSION_HEADER}: #{Wire::SDK_VERSION_VALUE}"]
      lines += ["Content-Type: application/json", "Content-Length: #{body.bytesize}"] unless body.nil?
      head = "#{lines.join("\r\n")}\r\n\r\n"
      body.nil? ? head : head.b + body.b
    end

    def write_all(socket, bytes, deadline)
      until bytes.empty?
        remaining(deadline)
        written = socket.write_nonblock(bytes, exception: false)
        if written == :wait_writable
          wait(socket, :write, deadline)
          next
        end
        bytes = bytes.byteslice(written..)
      end
    end

    # Reads more bytes into the connection's buffer; raises ConnectionBroken at the end of the stream.
    def fill(connection, deadline, started)
      loop do
        # Checked before every read, so an answer that keeps arriving still
        # ends at the deadline.
        remaining(deadline)
        chunk = connection.socket.read_nonblock(READ_SIZE, exception: false)
        case chunk
        when :wait_readable then wait(connection.socket, :read, deadline)
        when nil then raise ConnectionBroken, "the daemon closed the connection before answering"
        else
          started.call
          connection.buffer << chunk
          return
        end
      end
    end

    def read_line(connection, deadline, started)
      loop do
        line_end = connection.buffer.index("\r\n")
        unless line_end.nil?
          line = connection.buffer.byteslice(0, line_end)
          connection.buffer = connection.buffer.byteslice((line_end + 2)..)
          return line
        end
        if connection.buffer.bytesize > HEADER_LIMIT
          raise ConnectionBroken,
                "the daemon's answer has a header line that is too long"
        end

        fill(connection, deadline, started)
      end
    end

    def read_bytes(connection, count, deadline, started)
      fill(connection, deadline, started) while connection.buffer.bytesize < count
      bytes = connection.buffer.byteslice(0, count)
      connection.buffer = connection.buffer.byteslice(count..)
      bytes
    end

    def exchange(connection, reused, http_method, target, body, deadline, &started)
      connection.buffer = +"".b
      remaining(deadline)
      connect(connection.socket, deadline) unless reused
      write_all(connection.socket, request_bytes(http_method, target, body), deadline)
      loop do
        status, headers, version = read_head(connection, deadline, started)
        # An informational answer, such as 100 Continue, precedes the real one.
        next if status.between?(100, 199)

        answer_body, framed = read_body(connection, headers, deadline, started)
        reusable = framed && version == "HTTP/1.1" && headers["connection"].to_s.downcase != "close"
        return [
          Answer.new(status: status, client_version: headers[Wire::CLIENT_VERSION_HEADER].to_s,
                     body: answer_body), reusable
        ]
      end
    end

    def read_head(connection, deadline, started)
      status_line = read_line(connection, deadline, started)
      match = %r{\A(HTTP/1\.[01]) (\d{3})}.match(status_line)
      raise ConnectionBroken, "the daemon answered with #{status_line[0, 40].inspect}, not HTTP" if match.nil?

      headers = {}
      size = status_line.bytesize
      loop do
        line = read_line(connection, deadline, started)
        break if line.empty?

        size += line.bytesize
        raise ConnectionBroken, "the daemon's answer has too many headers" if size > HEADER_LIMIT

        header_name, header_value = line.split(":", 2)
        headers[header_name.strip.downcase] = header_value.strip unless header_value.nil?
      end
      [match[2].to_i, headers, match[1]]
    end

    def check_size(size)
      raise Error.new(ErrorCode::INVALID_RESPONSE, "the daemon's answer is too large") if size > Wire::RESPONSE_LIMIT
    end

    # The body and whether its length was framed, so the connection can carry another request.
    def read_body(connection, headers, deadline, started)
      if headers["transfer-encoding"].to_s.downcase.include?("chunked")
        [read_chunked(connection, deadline, started), true]
      elsif headers.key?("content-length")
        length = Integer(headers["content-length"], 10, exception: false)
        raise ConnectionBroken, "the daemon's answer has a bad Content-Length" if length.nil? || length.negative?

        check_size(length)
        [read_bytes(connection, length, deadline, started), true]
      else
        [read_to_end(connection, deadline, started), false]
      end
    end

    def read_chunked(connection, deadline, started)
      body = +"".b
      loop do
        size_line = read_line(connection, deadline, started)
        size = Integer(size_line.split(";", 2).first.to_s.strip, 16, exception: false)
        raise ConnectionBroken, "the daemon's answer has a bad chunk size" if size.nil? || size.negative?

        if size.zero?
          # Trailers, up to the blank line.
          nil until read_line(connection, deadline, started).empty?
          return body
        end
        check_size(body.bytesize + size)
        body << read_bytes(connection, size, deadline, started)
        raise ConnectionBroken, "the daemon's answer has a bad chunk end" unless read_line(connection, deadline,
                                                                                           started).empty?
      end
    end

    def read_to_end(connection, deadline, started)
      body = connection.buffer
      loop do
        check_size(body.bytesize)
        fill(connection, deadline, started)
      end
    rescue ConnectionBroken
      body
    end
  end
end
