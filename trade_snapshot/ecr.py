"""Strict, content-addressed FantasyPros Expert Consensus Ranking evidence."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from numbers import Real

from ._scenario_random import content_id
from .ecr_source import EcrHorizonEvidence, EcrSourceDetails
from .positions import normalize_player_position


_ECR_SCHEMA_VERSION = 4


class EcrPeriod(str, Enum):
    WEEKLY = "weekly"
    REST_OF_SEASON = "rest_of_season"


@dataclass(frozen=True, slots=True)
class EcrSourceProvenance:
    """Page-level evidence retained after position artifacts are merged."""

    league_scoring: str
    source_scoring: str
    capture_method: str
    captured_at: datetime
    source_updated_at: datetime | None
    source_updated_text: str
    source_details: EcrSourceDetails

    def __post_init__(self) -> None:
        league_scoring = _scoring("league_scoring", self.league_scoring)
        source_scoring = _scoring("source_scoring", self.source_scoring)
        capture_method = _enum_text(
            "capture_method", self.capture_method, {"visible_page", "official_api"}
        )
        captured_at = _aware("captured_at", self.captured_at)
        source_updated_at = (
            None
            if self.source_updated_at is None
            else _aware("source_updated_at", self.source_updated_at)
        )
        if source_updated_at is not None and source_updated_at > captured_at:
            raise ValueError("source_updated_at cannot be after captured_at")
        source_updated_text = _text("source_updated_text", self.source_updated_text)
        if not isinstance(self.source_details, EcrSourceDetails):
            raise ValueError("source_details must be EcrSourceDetails")
        for name, value in (
            ("league_scoring", league_scoring),
            ("source_scoring", source_scoring),
            ("capture_method", capture_method),
            ("captured_at", captured_at),
            ("source_updated_at", source_updated_at),
            ("source_updated_text", source_updated_text),
        ):
            object.__setattr__(self, name, value)

    def to_record(self) -> dict[str, object]:
        return {
            "capture_method": self.capture_method,
            "captured_at": _iso(self.captured_at),
            "league_scoring": self.league_scoring,
            "source_details": self.source_details.to_record(),
            "source_scoring": self.source_scoring,
            "source_updated_at": (
                None if self.source_updated_at is None else _iso(self.source_updated_at)
            ),
            "source_updated_text": self.source_updated_text,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "EcrSourceProvenance":
        fields = {
            "capture_method",
            "captured_at",
            "league_scoring",
            "source_details",
            "source_scoring",
            "source_updated_at",
            "source_updated_text",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("ECR source provenance fields are invalid")
        return cls(
            league_scoring=record["league_scoring"],
            source_scoring=record["source_scoring"],
            capture_method=record["capture_method"],
            captured_at=_parse_time("captured_at", record["captured_at"]),
            source_updated_at=(
                None
                if record["source_updated_at"] is None
                else _parse_time("source_updated_at", record["source_updated_at"])
            ),
            source_updated_text=record["source_updated_text"],
            source_details=EcrSourceDetails.from_record(record["source_details"]),
        )


@dataclass(frozen=True, slots=True)
class EcrExpertPanel:
    """The exact selected expert population behind one position page."""

    position: str
    expert_ids: tuple[str, ...]
    total_experts: int
    provenance: EcrSourceProvenance

    def __post_init__(self) -> None:
        position = _text("position", self.position).upper()
        experts = tuple(sorted(_unique_texts("expert_ids", self.expert_ids)))
        total = _integer("total_experts", self.total_experts, minimum=1)
        if not experts or total != len(experts):
            raise ValueError(
                "position expert panel must enumerate every selected expert"
            )
        if not isinstance(self.provenance, EcrSourceProvenance):
            raise ValueError("position expert panel must retain source provenance")
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "expert_ids", experts)
        object.__setattr__(self, "total_experts", total)

    def to_record(self) -> dict[str, object]:
        return {
            "expert_ids": list(self.expert_ids),
            "position": self.position,
            "provenance": self.provenance.to_record(),
            "total_experts": self.total_experts,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "EcrExpertPanel":
        if not isinstance(record, Mapping) or set(record) != {
            "expert_ids",
            "position",
            "provenance",
            "total_experts",
        }:
            raise ValueError("ECR expert panel fields are invalid")
        if not isinstance(record["expert_ids"], list):
            raise ValueError("ECR expert panel IDs must be a JSON array")
        return cls(
            record["position"],
            tuple(record["expert_ids"]),
            record["total_experts"],
            EcrSourceProvenance.from_record(record["provenance"]),
        )


@dataclass(frozen=True, slots=True)
class EcrPlayerRanking:
    canonical_player_id: str
    fantasypros_player_id: str
    position: str
    rank_ecr: int
    position_rank: int
    rank_min: int
    rank_max: int
    rank_average: float
    rank_stddev: float

    def __post_init__(self) -> None:
        for name in ("canonical_player_id", "fantasypros_player_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        position = _text("position", self.position).upper()
        object.__setattr__(self, "position", position)
        for name in ("rank_ecr", "position_rank", "rank_min", "rank_max"):
            object.__setattr__(self, name, _integer(name, getattr(self, name), minimum=1))
        if self.rank_min > self.rank_max:
            raise ValueError("rank_min cannot exceed rank_max")
        average = _finite("rank_average", self.rank_average, minimum=1)
        deviation = _finite("rank_stddev", self.rank_stddev, minimum=0)
        if not self.rank_min <= average <= self.rank_max:
            raise ValueError("rank_average must be between rank_min and rank_max")
        object.__setattr__(self, "rank_average", average)
        object.__setattr__(self, "rank_stddev", deviation)

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "fantasypros_player_id": self.fantasypros_player_id,
            "position": self.position,
            "position_rank": self.position_rank,
            "rank_average": self.rank_average,
            "rank_ecr": self.rank_ecr,
            "rank_max": self.rank_max,
            "rank_min": self.rank_min,
            "rank_stddev": self.rank_stddev,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "EcrPlayerRanking":
        keys = {
            "canonical_player_id",
            "fantasypros_player_id",
            "position",
            "position_rank",
            "rank_average",
            "rank_ecr",
            "rank_max",
            "rank_min",
            "rank_stddev",
        }
        if not isinstance(record, Mapping) or set(record) != keys:
            raise ValueError("ECR player record fields are invalid")
        return cls(**{name: record[name] for name in keys})


@dataclass(frozen=True, slots=True)
class EcrSnapshot:
    snapshot_id: str
    scoring_profile_id: str
    season: int
    as_of_week: int
    period: EcrPeriod
    captured_at: datetime
    source_updated_at: datetime | None
    expert_ids: tuple[str, ...]
    total_experts: int
    rankings: tuple[EcrPlayerRanking, ...]
    expert_panels: tuple[EcrExpertPanel, ...]
    ecr_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "scoring_profile_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(self, "season", _integer("season", self.season, minimum=2012))
        object.__setattr__(
            self, "as_of_week", _integer("as_of_week", self.as_of_week, minimum=1, maximum=25)
        )
        if not isinstance(self.period, EcrPeriod):
            raise ValueError("period must be an EcrPeriod")
        captured = _aware("captured_at", self.captured_at)
        updated = (
            None
            if self.source_updated_at is None
            else _aware("source_updated_at", self.source_updated_at)
        )
        if updated is not None and updated > captured:
            raise ValueError("source_updated_at cannot be after captured_at")
        experts = tuple(sorted(_unique_texts("expert_ids", self.expert_ids)))
        total = _integer("total_experts", self.total_experts, minimum=len(experts))
        rankings = _rankings(self.rankings)
        panels = _expert_panels(self.expert_panels)
        ranking_positions = {row.position for row in rankings}
        panel_positions = {row.position for row in panels}
        if panel_positions != ranking_positions:
            raise ValueError(
                "ECR expert panels must exactly cover ranking positions"
            )
        panel_experts = {
            expert_id
            for panel in panels
            for expert_id in panel.expert_ids
        }
        if set(experts) != panel_experts or total != len(panel_experts):
            raise ValueError(
                "aggregate ECR experts must equal the position-panel union"
            )
        if max(panel.provenance.captured_at for panel in panels) != captured:
            raise ValueError("ECR snapshot capture time must equal its latest position page")
        panel_updates = [panel.provenance.source_updated_at for panel in panels]
        expected_update = (
            None
            if any(value is None for value in panel_updates)
            else min(panel_updates)
        )
        if updated != expected_update:
            raise ValueError(
                "ECR snapshot update time must conservatively summarize its position pages"
            )
        for panel in panels:
            primary_source_rows = sum(
                count
                for source_position, count in (
                    panel.provenance.source_details.source_position_counts.items()
                )
                if normalize_player_position(source_position) == panel.position
            )
            ranking_count = sum(row.position == panel.position for row in rankings)
            if primary_source_rows != ranking_count:
                raise ValueError(
                    "ECR panel source counts must equal retained primary rankings"
                )
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "source_updated_at", updated)
        object.__setattr__(self, "expert_ids", experts)
        object.__setattr__(self, "total_experts", total)
        object.__setattr__(self, "rankings", rankings)
        object.__setattr__(self, "expert_panels", panels)
        object.__setattr__(self, "ecr_id", content_id("ecr", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        return {
            "as_of_week": self.as_of_week,
            "captured_at": _iso(self.captured_at),
            "expert_ids": list(self.expert_ids),
            "expert_panels": [row.to_record() for row in self.expert_panels],
            "period": self.period.value,
            "rankings": [row.to_record() for row in self.rankings],
            "scoring_profile_id": self.scoring_profile_id,
            "season": self.season,
            "snapshot_id": self.snapshot_id,
            "source_updated_at": (
                None if self.source_updated_at is None else _iso(self.source_updated_at)
            ),
            "total_experts": self.total_experts,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "fantasypros_ecr",
            "schema_version": _ECR_SCHEMA_VERSION,
            **self._content_record(),
            "ecr_id": self.ecr_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "EcrSnapshot":
        content_keys = {
            "as_of_week",
            "captured_at",
            "expert_ids",
            "expert_panels",
            "period",
            "rankings",
            "scoring_profile_id",
            "season",
            "snapshot_id",
            "source_updated_at",
            "total_experts",
        }
        if not isinstance(record, Mapping) or set(record) != content_keys | {
            "kind",
            "schema_version",
            "ecr_id",
        }:
            raise ValueError("ECR snapshot record fields are invalid")
        if (
            record["kind"] != "fantasypros_ecr"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != _ECR_SCHEMA_VERSION
        ):
            raise ValueError("ECR snapshot record kind or schema version is invalid")
        raw_rankings = record["rankings"]
        raw_experts = record["expert_ids"]
        raw_panels = record["expert_panels"]
        if (
            not isinstance(raw_rankings, list)
            or not isinstance(raw_experts, list)
            or not isinstance(raw_panels, list)
        ):
            raise ValueError(
                "ECR rankings, expert_ids, and expert_panels must be JSON arrays"
            )
        try:
            period = EcrPeriod(record["period"])
        except (TypeError, ValueError):
            raise ValueError("ECR period is invalid") from None
        snapshot = cls(
            snapshot_id=record["snapshot_id"],
            scoring_profile_id=record["scoring_profile_id"],
            season=record["season"],
            as_of_week=record["as_of_week"],
            period=period,
            captured_at=_parse_time("captured_at", record["captured_at"]),
            source_updated_at=(
                None
                if record["source_updated_at"] is None
                else _parse_time("source_updated_at", record["source_updated_at"])
            ),
            expert_ids=tuple(raw_experts),
            total_experts=record["total_experts"],
            rankings=tuple(EcrPlayerRanking.from_record(row) for row in raw_rankings),
            expert_panels=tuple(EcrExpertPanel.from_record(row) for row in raw_panels),
        )
        if record["ecr_id"] != snapshot.ecr_id:
            raise ValueError("ECR snapshot content does not match ecr_id")
        return snapshot


def _rankings(values: Iterable[EcrPlayerRanking]) -> tuple[EcrPlayerRanking, ...]:
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("rankings must be an iterable") from None
    if not rows or any(not isinstance(row, EcrPlayerRanking) for row in rows):
        raise ValueError("rankings must contain EcrPlayerRanking values")
    canonical = tuple(row.canonical_player_id for row in rows)
    provider = tuple(row.fantasypros_player_id for row in rows)
    if len(set(canonical)) != len(rows) or len(set(provider)) != len(rows):
        raise ValueError("ECR rankings contain a duplicate player identity")
    return tuple(sorted(rows, key=lambda row: row.canonical_player_id))


def _expert_panels(values: Iterable[EcrExpertPanel]) -> tuple[EcrExpertPanel, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("expert_panels must be an iterable")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("expert_panels must be an iterable") from None
    if any(not isinstance(row, EcrExpertPanel) for row in rows):
        raise ValueError("expert_panels must contain EcrExpertPanel values")
    if len({row.position for row in rows}) != len(rows):
        raise ValueError("ECR expert panels contain a duplicate position")
    return tuple(sorted(rows, key=lambda row: row.position))


def _unique_texts(name: str, values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of strings")
    try:
        result = tuple(_text(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable of strings") from None
    if len(set(result)) != len(result):
        raise ValueError(f"{name} contains a duplicate")
    return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _scoring(name: str, value: object) -> str:
    scoring = _text(name, value).upper()
    if scoring not in {"STD", "HALF", "PPR"}:
        raise ValueError(f"{name} must be STD, HALF, or PPR")
    return scoring


def _enum_text(name: str, value: object, allowed: set[str]) -> str:
    text = _text(name, value).casefold()
    if text not in allowed:
        raise ValueError(f"{name} is invalid")
    return text


def _integer(name: str, value: object, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} cannot exceed {maximum}")
    return value


def _finite(name: str, value: object, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number of at least {minimum}")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number of at least {minimum}") from None
    if not isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be a finite number of at least {minimum}")
    return result


def _aware(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    return _aware(name, parsed)
