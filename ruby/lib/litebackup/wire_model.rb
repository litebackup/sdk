# frozen_string_literal: true

require "time"

module Litebackup
  # Builds the generated models from decoded JSON. Unknown fields are
  # ignored and missing ones get their type's zero value, so a newer daemon's
  # answers keep decoding; enum fields keep values this SDK does not know.
  module WireModel
    # A model from a decoded JSON object.
    def from_wire(value)
      raise TypeError, "expected an object for #{name}, got #{value.class}" unless value.is_a?(Hash)

      fields = self::WIRE_FIELDS.to_h do |field, (decoding, required)|
        key = field.to_s
        if value.key?(key)
          [field, WireModel.decode_value(decoding, value[key])]
        else
          [field, required ? WireModel.zero_value(decoding) : nil]
        end
      end
      new(**fields)
    end

    def self.decode_value(decoding, value)
      return nil if value.nil?

      case decoding
      when :time then value.is_a?(String) ? Time.iso8601(value) : value
      when :float then value.is_a?(Integer) ? value.to_f : value
      when Array then decode_composite(decoding, value)
      else value
      end
    end

    def self.decode_composite(decoding, value)
      shape, inner = decoding
      return Models.const_get(inner).from_wire(value) if shape == :model
      raise TypeError, "expected a list, got #{value.class}" unless value.is_a?(Array)

      value.map { |element| decode_value(inner, element) }
    end

    def self.zero_value(decoding)
      case decoding
      when :string then ""
      when :integer then 0
      when :float then 0.0
      when :boolean then false
      when :hash then {}
      when Array then decoding.first == :list ? [] : Models.const_get(decoding.last).from_wire({})
      end
    end
  end
end
