"""Decoding JSON answers into the generated dataclasses.

Unknown fields are ignored and missing ones take their type's zero value, so a
newer daemon's answers keep decoding. Literal fields keep values this SDK does
not know.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from datetime import datetime
from typing import Any, TypeVar

Model = TypeVar("Model")


_field_type_cache: dict[type[Any], dict[str, Any]] = {}


def _field_types(cls: type[Any]) -> dict[str, Any]:
    hints = _field_type_cache.get(cls)
    if hints is None:
        hints = _field_type_cache[cls] = typing.get_type_hints(cls)
    return hints


def _zero(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        return None
    if origin is list or annotation is list:
        return []
    if origin is dict or annotation is dict:
        return {}
    if origin is typing.Literal:
        return ""
    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        return decode(annotation, {})
    return {str: "", int: 0, float: 0.0, bool: False}.get(annotation)


def _value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        candidates = [argument for argument in typing.get_args(annotation) if argument is not type(None)]
        return _value(candidates[0], value) if len(candidates) == 1 else value
    if origin is list:
        (item,) = typing.get_args(annotation)
        return [_value(item, element) for element in value]
    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        return decode(annotation, value)
    if annotation is datetime and isinstance(value, str):
        return datetime.fromisoformat(value)
    if annotation is float and isinstance(value, int):
        return float(value)
    return value


def decode(cls: type[Model], value: Any) -> Model:
    """Builds a generated dataclass from decoded JSON."""
    if not isinstance(value, dict):
        raise TypeError(f"expected an object for {cls.__name__}, got {type(value).__name__}")
    hints = _field_types(cls)
    arguments: dict[str, Any] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        annotation = hints[field.name]
        if field.name in value:
            arguments[field.name] = _value(annotation, value[field.name])
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            arguments[field.name] = _zero(annotation)
    return cls(**arguments)
