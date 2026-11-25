# frozen_string_literal: true

require "socket"
require "tmpdir"
require "uri"
require "json"

# A fake litebackup daemon on a unix socket, for the contract fixtures and
# unit tests: HTTP/1.1 with keep-alive and Content-Length bodies, enough for
# the SDK. It answers the connect probe itself and passes every other
# request to the reply callable, which returns [status, content type, body].
class ContractDaemon
  # One request the daemon received.
  Received = Data.define(:http_method, :path, :query, :body, :headers) do
    def json
      body.empty? ? nil : JSON.parse(body)
    end
  end

  STATUS_BODY = '{"session":"contract","server":"http://example","client_version":"1.6.0",' \
                '"session_id":"s","watch":"inotify","databases":[]}'

  attr_reader :socket_path, :received, :sdk_versions, :directory

  def initialize(client_version: "1.6.0", &reply)
    @reply = reply
    @client_version = client_version
    @directory = Dir.mktmpdir("lbr-")
    @socket_path = File.join(@directory, "client.sock")
    @received = []
    @sdk_versions = []
    @lock = Mutex.new
    @connections = []
    @stopped = true
    serve
  end

  def stopped?
    @stopped
  end

  # Listens on the socket and answers there until stop.
  def serve
    @stopped = false
    @server = UNIXServer.new(@socket_path)
    @acceptor = Thread.new { accept_connections(@server) }
  end

  # Closes the socket and every connection, as a daemon that exits does;
  # calls then fail with connection_failed until serve.
  def stop
    return if @stopped

    @stopped = true
    @server.close
    @acceptor.join
    @lock.synchronize { @connections.dup }.each do |connection|
      connection.shutdown(Socket::SHUT_RDWR)
    rescue IOError, SystemCallError
      nil
    end
    FileUtils.rm_f(@socket_path)
  end

  def close
    stop
    FileUtils.rm_rf(@directory)
  end

  # The requests other than the connect probe, as their method, path, and query.
  def requests
    @lock.synchronize { @received.dup }
  end

  private

  def accept_connections(server)
    loop do
      connection = server.accept
      @lock.synchronize { @connections << connection }
      Thread.new { answer_connection(connection) }
    end
  rescue IOError, SystemCallError
    nil
  end

  def answer_connection(connection)
    loop do
      request = read_request(connection)
      break if request.nil? || @stopped

      status, content_type, body = answer(request)
      # The daemon stopped while this request waited: it gets no answer.
      break if @stopped

      head = ["HTTP/1.1 #{status} Answer"]
      head << "Litebackup-Client-Version: #{@client_version}" unless @client_version.nil?
      head << "Content-Type: #{content_type}" << "Content-Length: #{body.bytesize}"
      connection.write("#{head.join("\r\n")}\r\n\r\n", body)
    end
  rescue IOError, SystemCallError
    nil
  ensure
    @lock.synchronize { @connections.delete(connection) }
    connection.close
  end

  def answer(request)
    @lock.synchronize { @sdk_versions << request.headers["litebackup-sdk-version"].to_s }
    if request.http_method == "GET" && request.path == "/v1/status/show" && request.query == { "limit" => "1" }
      return [200, "application/json", STATUS_BODY]
    end

    @lock.synchronize { @received << request }
    @reply.call(request)
  end

  def read_request(connection)
    request_line = connection.gets("\r\n")
    return nil if request_line.nil?

    http_method, target = request_line.split(" ", 3)
    headers = {}
    while (line = connection.gets("\r\n")) && line != "\r\n"
      name, value = line.split(":", 2)
      headers[name.strip.downcase] = value.strip
    end
    body = connection.read(headers.fetch("content-length", "0").to_i).to_s
    path, query = target.split("?", 2)
    Received.new(http_method: http_method, path: path, query: URI.decode_www_form(query.to_s).to_h,
                 body: body, headers: headers)
  end
end
