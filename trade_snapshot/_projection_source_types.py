"""Small immutable value types shared by projection-source provenance."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from .capture_schema import CaptureProvider, ProjectionTableSpec, RankingHorizon

_CONTENT_ID = re.compile(r"^[a-z][a-z0-9-]*_[0-9a-f]{64}$")


class ProjectionAttemptStatus(str, Enum):
    CAPTURED = "captured"
    NOT_PUBLISHED = "not_published"
    UNAVAILABLE = "unavailable"


class ProjectionAttemptReason(str, Enum):
    CAPTURED = "captured"
    SOURCE_NOT_PUBLISHED = "source_not_published"
    PROVIDER_PAGE_UNAVAILABLE = "provider_page_unavailable"
    PROVIDER_LAYOUT_UNSUPPORTED = "provider_layout_unsupported"


class ProjectionPointBasis(str, Enum):
    PROVIDER_TOTAL = "provider_total"
    LOCALLY_RECOMPUTED = "locally_recomputed"


class HostScoringCompatibility(str, Enum):
    BASE_FORMAT_ONLY = "base_format_only"
    EXACT_HOST_RULES = "exact_host_rules"


class ProjectionInputPresence(str, Enum):
    """How a normalized input is evidenced by its complete source capture."""

    SOURCE_ROW = "source_row"
    OMITTED_FROM_COMPLETE_CAPTURE = "omitted_from_complete_capture"


@dataclass(frozen=True, slots=True)
class ProjectionSourceAttempt:
    """Sanitized, timestamped outcome for one requested projection page."""

    task_id: str
    provider: CaptureProvider | str
    season: int
    week: int
    horizon: RankingHorizon | str
    scoring: str
    position_scope: tuple[str, ...]
    attempted_at: datetime
    status: ProjectionAttemptStatus | str
    reason_code: ProjectionAttemptReason | str
    artifact_id: str | None = None

    def __post_init__(self) -> None:
        task_id = _content_id("task_id", self.task_id, "captask")
        try:
            provider = CaptureProvider(self.provider)
            status = ProjectionAttemptStatus(self.status)
            reason = ProjectionAttemptReason(self.reason_code)
        except (TypeError, ValueError):
            raise ValueError("projection source attempt enum value is invalid") from None
        season = _integer("season", self.season, minimum=2012, maximum=9999)
        week = _integer("week", self.week, minimum=1, maximum=25)
        dimensions = ProjectionTableSpec(self.horizon, self.scoring, self.position_scope)
        attempted_at = _aware("attempted_at", self.attempted_at)
        artifact_id = (
            None
            if self.artifact_id is None
            else _content_id("artifact_id", self.artifact_id, "captable")
        )
        allowed = {
            ProjectionAttemptStatus.CAPTURED: {ProjectionAttemptReason.CAPTURED},
            ProjectionAttemptStatus.NOT_PUBLISHED: {
                ProjectionAttemptReason.SOURCE_NOT_PUBLISHED
            },
            ProjectionAttemptStatus.UNAVAILABLE: {
                ProjectionAttemptReason.PROVIDER_PAGE_UNAVAILABLE,
                ProjectionAttemptReason.PROVIDER_LAYOUT_UNSUPPORTED,
            },
        }
        if reason not in allowed[status]:
            raise ValueError("projection source attempt status and reason do not match")
        if (status is ProjectionAttemptStatus.CAPTURED) != (artifact_id is not None):
            raise ValueError("only a captured projection attempt may have an artifact_id")
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "week", week)
        object.__setattr__(self, "horizon", dimensions.horizon)
        object.__setattr__(self, "scoring", dimensions.scoring)
        object.__setattr__(self, "position_scope", dimensions.position_scope)
        object.__setattr__(self, "attempted_at", attempted_at)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", reason)
        object.__setattr__(self, "artifact_id", artifact_id)

    def to_record(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "attempted_at": _iso(self.attempted_at),
            "horizon": self.horizon.value,
            "position_scope": list(self.position_scope),
            "provider": self.provider.value,
            "reason_code": self.reason_code.value,
            "scoring": self.scoring,
            "season": self.season,
            "status": self.status.value,
            "task_id": self.task_id,
            "week": self.week,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionSourceAttempt":
        fields = {
            "artifact_id",
            "attempted_at",
            "horizon",
            "position_scope",
            "provider",
            "reason_code",
            "scoring",
            "season",
            "status",
            "task_id",
            "week",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("projection source attempt fields are invalid")
        positions = record["position_scope"]
        if not isinstance(positions, list):
            raise ValueError("projection source attempt position_scope must be an array")
        return cls(
            task_id=record["task_id"],
            provider=record["provider"],
            season=record["season"],
            week=record["week"],
            horizon=record["horizon"],
            scoring=record["scoring"],
            position_scope=tuple(positions),
            attempted_at=_parse_time("attempted_at", record["attempted_at"]),
            status=record["status"],
            reason_code=record["reason_code"],
            artifact_id=record["artifact_id"],
        )


@dataclass(frozen=True, slots=True)
class ProjectionInputBinding:
    """Bind one normalized row to the provider player row that produced it."""

    canonical_player_id: str
    provider_player_id: str
    projection_input_id: str
    presence: ProjectionInputPresence | str = ProjectionInputPresence.SOURCE_ROW

    def __post_init__(self) -> None:
        for name in ("canonical_player_id", "provider_player_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(
            self,
            "projection_input_id",
            _content_id("projection_input_id", self.projection_input_id, "projection-input"),
        )
        try:
            presence = ProjectionInputPresence(self.presence)
        except (TypeError, ValueError):
            raise ValueError("projection input presence is invalid") from None
        object.__setattr__(self, "presence", presence)

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "presence": self.presence.value,
            "projection_input_id": self.projection_input_id,
            "provider_player_id": self.provider_player_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionInputBinding":
        fields = {
            "canonical_player_id",
            "presence",
            "projection_input_id",
            "provider_player_id",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("projection input binding fields are invalid")
        return cls(**{name: record[name] for name in fields})


def _typed(name, values, expected_type):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(not isinstance(value, expected_type) for value in result):
        raise ValueError(f"{name} must contain {expected_type.__name__} values")
    return result


def _content_id(name, value, prefix):
    value = _text(name, value)
    if not _CONTENT_ID.fullmatch(value) or not value.startswith(f"{prefix}_"):
        raise ValueError(f"{name} must be a {prefix} content ID")
    return value


def _text(name, value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty text without outer whitespace")
    return value


def _integer(name, value, *, minimum, maximum):
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{name} is outside its supported range")
    return value


def _aware(name, value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _parse_time(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    return _aware(name, parsed)


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


__all__ = (
    "HostScoringCompatibility",
    "ProjectionAttemptReason",
    "ProjectionAttemptStatus",
    "ProjectionInputBinding",
    "ProjectionInputPresence",
    "ProjectionPointBasis",
    "ProjectionSourceAttempt",
)
