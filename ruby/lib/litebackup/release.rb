# frozen_string_literal: true

require "json"

module Litebackup
  # Verifying Litebackup's signed package metadata, as
  # https://litebackup.com/pkg/metadata serves it. The release is always read
  # from its signed document, never from the unsigned listing. Needs Ruby's
  # openssl, which is loaded on first use.
  module Release
    # Litebackup's Ed25519 release keys by key ID, base64url without padding,
    # as the litebackup command checks them.
    RELEASE_KEYS = {
      "01a1ead6-4b94-79ca-81f4-3a67c112bc0b" => "t3C_3l71PimyhkPZOzJ_Xi4dOuHJ1CmSdDZ8lTm0vNI"
    }.freeze
    COMMAND_PACKAGE = "cli"
    STABLE_CHANNEL = "stable"
    SIGNATURE_CONTEXT = "clor-apps-v1\0"
    SIGNATURE_SEPARATOR = "\0"
    # The DER prefix of an Ed25519 public key's SubjectPublicKeyInfo, before its 32 bytes.
    ED25519_PUBLIC_KEY_PREFIX = ["302a300506032b6570032100"].pack("H*").freeze

    # A release whose signature does not match Litebackup's release keys.
    class SignatureError < StandardError
      def initialize(message = "the release signature does not match Litebackup's release keys")
        super
      end
    end

    # A package or release the metadata does not list.
    class NotListed < StandardError; end

    # One published file of a release.
    ReleaseFile = Data.define(:name, :filename, :url, :sha256, :size, :os, :architecture)

    # A package release read from its verified signed document.
    PackageRelease = Data.define(:version, :channels, :files) do
      def file(name)
        files.find { |candidate| candidate.name == name } ||
          raise(NotListed, "release #{version} has no file #{name}")
      end
    end

    module_function

    def base64url(text)
      # base64 left Ruby's default gems in 3.4, so the gem decodes it with pack.
      (text.tr("-_", "+/") + ("=" * (-text.length % 4))).unpack1("m0")
    end

    # The signed document, decoded, once its signature matches one of keys.
    def verify(signed, keys)
      require "openssl"
      raise SignatureError unless signed.is_a?(Hash) && keys.key?(signed["key_id"])

      begin
        key = OpenSSL::PKey.read(ED25519_PUBLIC_KEY_PREFIX + base64url(keys.fetch(signed["key_id"])))
        document = base64url(signed.fetch("document"))
        message = SIGNATURE_CONTEXT.b + signed["key_id"].b + SIGNATURE_SEPARATOR.b + document.b
        raise SignatureError unless key.verify(nil, base64url(signed.fetch("signature")), message)

        decoded = JSON.parse(document)
      rescue OpenSSL::PKey::PKeyError, ArgumentError, KeyError, TypeError, JSON::ParserError
        raise SignatureError
      end
      raise SignatureError unless decoded.is_a?(Hash)

      decoded
    end

    # Reads package metadata and returns the selected release from its signed
    # document: the release with version, or else the newest on channel.
    # Raises SignatureError when the signature does not verify or the signed
    # version is not the listed one, and NotListed for a missing package or release.
    def find_release(metadata, package: COMMAND_PACKAGE, version: nil, channel: STABLE_CHANNEL, keys: RELEASE_KEYS)
      listing = metadata.is_a?(Hash) ? metadata : JSON.parse(metadata)
      listed = Array(listing["packages"]).find { |candidate| candidate["slug"] == package }
      raise NotListed, "no package #{package}" if listed.nil?

      candidate = Array(listed["releases"]).find do |release|
        version.nil? ? Array(release["channels"]).include?(channel) : release["version"] == version
      end
      if candidate.nil?
        raise NotListed, "package #{package} has no release #{version.nil? ? "on #{channel}" : version}"
      end

      document = verify(candidate["signature"], keys)
      raise SignatureError if document["version"] != candidate["version"]

      files = Array(document["artifacts"]).map do |artifact|
        ReleaseFile.new(name: artifact.fetch("name"), filename: artifact.fetch("filename"), url: artifact.fetch("url"),
                        sha256: artifact.fetch("sha256"), size: artifact.fetch("size"),
                        os: artifact.fetch("os", ""), architecture: artifact.fetch("architecture", ""))
      end
      PackageRelease.new(version: document.fetch("version"), channels: Array(candidate["channels"]), files: files)
    end
  end
end
