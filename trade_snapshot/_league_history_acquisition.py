"""Typed provenance for one league-history collection attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class HistoryAcquisitionOutcome(str, Enum):
    CAPTURED_COMPLETE = "captured_complete"
    CAPTURED_PARTIAL = "captured_partial"
    LEGACY_UNKNOWN = "legacy_unknown"


@dataclass(frozen=True, slots=True)
class HistorySkipCount:
    """A bounded aggregate of source rows intentionally not normalized."""

    reason_code: str
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reason_code", _identifier("skip reason_code", self.reason_code)
        )
        _nonnegative_integer("skip count", self.count)
        if self.count == 0:
            raise ValueError("skip count must be positive")

    def to_record(self) -> dict[str, object]:
        return {"count": self.count, "reason_code": self.reason_code}

    @classmethod
    def from_record(cls, value: object) -> "HistorySkipCount":
        row = _record(value, {"count", "reason_code"}, "history skip count")
        return cls(row["reason_code"], row["count"])


@dataclass(frozen=True, slots=True)
class HistoryAcquisitionEvidence:
    """What was requested, returned, retained, and omitted in one capture."""

    provider: str
    attempted_at: datetime
    outcome: HistoryAcquisitionOutcome | str
    completeness_policy: str
    normalized_transaction_count: int
    returned_transaction_count: int | None
    transaction_limit: int | None
    earliest_source_event_at: datetime | None
    latest_source_event_at: datetime | None
    skipped: tuple[HistorySkipCount, ...] = ()

    def __post_init__(self) -> None:
        provider = _identifier("history provider", self.provider).casefold()
        attempted_at = _aware("history attempted_at", self.attempted_at)
        try:
            outcome = HistoryAcquisitionOutcome(self.outcome)
        except (TypeError, ValueError):
            raise ValueError("history acquisition outcome is unsupported") from None
        policy = _identifier("history completeness_policy", self.completeness_policy)
        normalized = _nonnegative_integer(
            "normalized_transaction_count", self.normalized_transaction_count
        )
        returned = _optional_nonnegative_integer(
            "returned_transaction_count", self.returned_transaction_count
        )
        limit = _optional_positive_integer("transaction_limit", self.transaction_limit)
        earliest = _optional_aware(
            "earliest_source_event_at", self.earliest_source_event_at
        )
        latest = _optional_aware("latest_source_event_at", self.latest_source_event_at)
        try:
            skipped = tuple(self.skipped)
        except TypeError:
            raise ValueError("skipped must contain HistorySkipCount values") from None
        if any(not isinstance(row, HistorySkipCount) for row in skipped):
            raise ValueError("skipped must contain HistorySkipCount values")
        skipped = tuple(sorted(skipped, key=lambda row: row.reason_code))
        if len({row.reason_code for row in skipped}) != len(skipped):
            raise ValueError("history skips contain a duplicate reason_code")

        if outcome is HistoryAcquisitionOutcome.LEGACY_UNKNOWN:
            if returned is not None or limit is not None or skipped:
                raise ValueError("legacy acquisition cannot invent return or skip evidence")
        else:
            if returned is None or limit is None:
                raise ValueError("captured acquisition requires return count and limit")
            if normalized > returned or returned > limit:
                raise ValueError("history acquisition counts are inconsistent")
            if sum(row.count for row in skipped) != returned - normalized:
                raise ValueError("history skip counts do not explain omitted source rows")
            is_complete = outcome is HistoryAcquisitionOutcome.CAPTURED_COMPLETE
            if is_complete != (returned < limit):
                raise ValueError("history acquisition outcome conflicts with source limit")

        if (earliest is None) != (latest is None):
            raise ValueError("source event bounds must both be known or both be null")
        if earliest is not None:
            if earliest > latest or latest > attempted_at:
                raise ValueError("source event bounds are not ordered")
        elif normalized and outcome is not HistoryAcquisitionOutcome.LEGACY_UNKNOWN:
            raise ValueError("normalized source events require source time bounds")

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "attempted_at", attempted_at)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "completeness_policy", policy)
        object.__setattr__(self, "normalized_transaction_count", normalized)
        object.__setattr__(self, "returned_transaction_count", returned)
        object.__setattr__(self, "transaction_limit", limit)
        object.__setattr__(self, "earliest_source_event_at", earliest)
        object.__setattr__(self, "latest_source_event_at", latest)
        object.__setattr__(self, "skipped", skipped)

    @property
    def history_complete(self) -> bool | None:
        if self.outcome is HistoryAcquisitionOutcome.LEGACY_UNKNOWN:
            return None
        return self.outcome is HistoryAcquisitionOutcome.CAPTURED_COMPLETE

    @property
    def skipped_transaction_count(self) -> int | None:
        if self.outcome is HistoryAcquisitionOutcome.LEGACY_UNKNOWN:
            return None
        return sum(row.count for row in self.skipped)

    def to_record(self) -> dict[str, object]:
        return {
            "attempted_at": _timestamp(self.attempted_at),
            "completeness_policy": self.completeness_policy,
            "earliest_source_event_at": _optional_timestamp(
                self.earliest_source_event_at
            ),
            "latest_source_event_at": _optional_timestamp(self.latest_source_event_at),
            "normalized_transaction_count": self.normalized_transaction_count,
            "outcome": self.outcome.value,
            "provider": self.provider,
            "returned_transaction_count": self.returned_transaction_count,
            "skipped": [row.to_record() for row in self.skipped],
            "transaction_limit": self.transaction_limit,
        }

    @classmethod
    def from_record(cls, value: object) -> "HistoryAcquisitionEvidence":
        row = _record(
            value,
            {
                "attempted_at",
                "completeness_policy",
                "earliest_source_event_at",
                "latest_source_event_at",
                "normalized_transaction_count",
                "outcome",
                "provider",
                "returned_transaction_count",
                "skipped",
                "transaction_limit",
            },
            "history acquisition evidence",
        )
        skipped = row["skipped"]
        if not isinstance(skipped, list):
            raise ValueError("history acquisition skipped must be an array")
        return cls(
            row["provider"],
            _datetime("attempted_at", row["attempted_at"]),
            row["outcome"],
            row["completeness_policy"],
            row["normalized_transaction_count"],
            row["returned_transaction_count"],
            row["transaction_limit"],
            _optional_datetime(
                "earliest_source_event_at", row["earliest_source_event_at"]
            ),
            _optional_datetime(
                "latest_source_event_at", row["latest_source_event_at"]
            ),
            tuple(HistorySkipCount.from_record(item) for item in skipped),
        )

    @classmethod
    def legacy_unknown(
        cls,
        attempted_at: datetime,
        normalized_transaction_count: int,
        *,
        earliest_source_event_at: datetime | None = None,
        latest_source_event_at: datetime | None = None,
    ) -> "HistoryAcquisitionEvidence":
        return cls(
            "legacy",
            attempted_at,
            HistoryAcquisitionOutcome.LEGACY_UNKNOWN,
            "legacy_v1_unknown",
            normalized_transaction_count,
            None,
            None,
            earliest_source_event_at,
            latest_source_event_at,
        )


def _record(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return value


def _identifier(name: str, value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.strip()):
        raise ValueError(f"{name} must be a bounded identifier")
    return value.strip()


def _nonnegative_integer(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_integer(name: str, value: object) -> int | None:
    return None if value is None else _nonnegative_integer(name, value)


def _optional_positive_integer(name: str, value: object) -> int | None:
    if value is None:
        return None
    result = _nonnegative_integer(name, value)
    if result == 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _aware(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _optional_aware(name: str, value: object) -> datetime | None:
    return None if value is None else _aware(name, value)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _datetime(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    result = _aware(name, parsed)
    if _timestamp(result) != value:
        raise ValueError(f"{name} must use the canonical UTC timestamp format")
    return result


def _optional_datetime(name: str, value: object) -> datetime | None:
    return None if value is None else _datetime(name, value)


__all__ = (
    "HistoryAcquisitionEvidence",
    "HistoryAcquisitionOutcome",
    "HistorySkipCount",
)
