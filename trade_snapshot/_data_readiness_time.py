"""UTC timestamp normalization shared by readiness contracts and evidence."""

from datetime import datetime, timezone


def aware_datetime(name, value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def timestamp_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_timestamp_text(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("readiness timestamp must be ISO-8601 text") from None
    return aware_datetime("readiness timestamp", parsed)
