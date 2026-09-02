"""Small strict-record helpers shared by capture schema modules."""

from collections.abc import Mapping

from ._capture_common import require_text


def enum_value(enum_type, name: str, value: object):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from None


def exact_fields(record: object, expected: set[str], name: str) -> None:
    if not isinstance(record, Mapping) or set(record) != expected:
        raise ValueError(f"{name} fields do not match the schema")


def text_set(name: str, values: object, *, uppercase: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of strings")
    try:
        normalized = tuple(require_text(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable of strings") from None
    if uppercase:
        normalized = tuple(value.upper() for value in normalized)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must contain unique strings")
    return tuple(sorted(normalized))


def optional_text_set(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of strings")
    try:
        normalized = tuple(require_text(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable of strings") from None
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must contain unique strings")
    return tuple(sorted(normalized))


__all__ = ("enum_value", "exact_fields", "optional_text_set", "text_set")
