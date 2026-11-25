# frozen_string_literal: true

module Litebackup
  # Holds the credential a daemon uses when it starts: an API key, or a join
  # token the daemon exchanges once for a key of the machine, then @ and the
  # server's address.
  API_KEY_ENVIRONMENT = "LITEBACKUP_API_KEY"

  # Telling an API key from a join token, as the daemon does.
  module Credential
    # Join and invite tokens start with longer prefixes of an API key's.
    API_KEY_PREFIX = "litebackup_"
    JOIN_TOKEN_PREFIX = "litebackup_join_"
    INVITE_PREFIX = "litebackup_invite_"
    API_KEY = "api_key"
    JOIN_TOKEN = "join_token"

    # The server address a credential names, as the daemon checks it: an
    # optional http:// or https:// in any case, a host name or a bracketed
    # IPv6 address, an optional port, and an optional path without a query or
    # fragment, of ASCII URL characters and percent escapes.
    SERVER_ADDRESS = %r{
      \A
      (?:[Hh][Tt][Tt][Pp][Ss]?://)?
      (?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])
      (?::[0-9]{1,5})?
      (?:/(?:[A-Za-z0-9._~!$&'()*+,;=:/-]|%[0-9A-Fa-f]{2})*)?
      \z
    }x

    module_function

    # Tells an API key from a join token by prefix: "api_key" or
    # "join_token", or nil for an empty credential, which leaves the daemon
    # its saved key. Raises invalid_argument, without repeating the
    # credential, for an invite token, a credential with neither prefix, or
    # one without @ and the server's address.
    def credential_type(key_and_server)
      stripped = key_and_server.to_s.strip
      return nil if stripped.empty?

      separator = stripped.rindex("@")
      secret = separator.nil? ? "" : stripped[0...separator]
      server = separator.nil? ? "" : stripped[(separator + 1)..]
      if secret.empty? || !SERVER_ADDRESS.match?(server)
        raise Litebackup.invalid_argument(
          "expected <API_KEY>@<HOST>: an API key or a join token from Machines → Add machine, " \
          "then @ and the server's address: a host, or an http:// or https:// URL of one"
        )
      end
      if secret.start_with?(INVITE_PREFIX)
        raise Litebackup.invalid_argument(
          "this is an invite token, which signs a person in to the console; " \
          "use an API key or a join token from Machines → Add machine"
        )
      end
      return JOIN_TOKEN if secret.start_with?(JOIN_TOKEN_PREFIX)
      return API_KEY if secret.start_with?(API_KEY_PREFIX)

      raise Litebackup.invalid_argument(
        "expected an API key or a join token, which start with #{API_KEY_PREFIX}; get one from Machines → Add machine"
      )
    end
  end
end
