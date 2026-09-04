"""Strict normalized projection records for offline calculations."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math
from numbers import Real
import re
import unicodedata


__all__ = (
    "ProjectionStatus",
    "ProviderStatusObservation",
    "ProviderStatusScope",
    "WeeklyProjectionOrigin",
    "RemainingSeasonOrigin",
    "RemainingSeasonProjection",
    "WeeklyProjection",
    "derive_remaining_season",
)


class ProjectionStatus(str, Enum):
    """Whether a provider supplied a usable projection for a player period."""

    OBSERVED = "observed"
    BYE = "bye"
    NOT_APPLICABLE = "not_applicable"
    NOT_PUBLISHED = "not_published"
    PARSE_ERROR = "parse_error"
    UNMATCHED_PLAYER = "unmatched_player"


class RemainingSeasonOrigin(str, Enum):
    """How a remaining-season total was obtained."""

    PROVIDER_PUBLISHED = "provider_published"
    DERIVED_WEEKLY = "derived_weekly"
    DERIVED_FULL_SEASON = "derived_full_season"


class WeeklyProjectionOrigin(str, Enum):
    """Whether a weekly value was published or allocated from a ROS total."""

    PROVIDER_PUBLISHED = "provider_published"
    DERIVED_REST_OF_SEASON = "derived_rest_of_season"


class ProviderStatusScope(str, Enum):
    """The provider page on which a non-authoritative designation appeared."""

    WEEKLY = "weekly"
    REST_OF_SEASON = "ros"


@dataclass(frozen=True, slots=True)
class ProviderStatusObservation:
    """A provider's captured label, not a determination that a player will play."""

    designation: str
    captured_at: datetime
    source_scope: ProviderStatusScope
    source_week: int | None = None

    def __post_init__(self) -> None:
        _validate_designation(self.designation)
        _require_aware_datetime("provider status captured_at", self.captured_at)
        if not isinstance(self.source_scope, ProviderStatusScope):
            raise ValueError(
                "provider status source_scope must be a ProviderStatusScope"
            )
        if self.source_scope is ProviderStatusScope.WEEKLY:
            _require_int(
                "provider status source_week",
                self.source_week,
                minimum=1,
                maximum=25,
            )
        elif self.source_week is not None:
            raise ValueError("ROS provider status observation cannot have source_week")


@dataclass(frozen=True, slots=True, eq=False)
class _FrozenStats(Mapping[str, float]):
    """A truly immutable, hashable projected-stat mapping."""

    _items: tuple[tuple[str, float], ...]

    def __init__(self, items: Iterable[tuple[str, float]]) -> None:
        object.__setattr__(self, "_items", tuple(items))

    def __getitem__(self, key: str) -> float:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (name for name, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)

    def __deepcopy__(self, memo):
        return self

    def to_dict(self) -> dict[str, float]:
        return dict(self._items)


@dataclass(frozen=True)
class WeeklyProjection:
    """One provider's projection for one canonical player and NFL week."""

    canonical_player_id: str | None
    snapshot_id: str
    scoring_profile_id: str
    provider: str
    provider_player_id: str
    season: int
    week: int
    status: ProjectionStatus
    captured_at: datetime
    projected_fantasy_points: float | None = None
    raw_projected_stats: Mapping[str, float] = field(default_factory=dict)
    nfl_team_id: str | None = None
    nfl_game_id: str | None = None
    opponent_team_id: str | None = None
    is_home: bool | None = None
    source_published_at: datetime | None = None
    origin: WeeklyProjectionOrigin = WeeklyProjectionOrigin.PROVIDER_PUBLISHED
    provider_status_observations: tuple[ProviderStatusObservation, ...] = ()

    def __post_init__(self) -> None:
        _validate_identity(self)
        if not isinstance(self.origin, WeeklyProjectionOrigin):
            raise ValueError("origin must be a WeeklyProjectionOrigin")
        _require_int("season", self.season, minimum=2012, maximum=None)
        _require_int("week", self.week, minimum=1, maximum=25)
        _validate_times(self.source_published_at, self.captured_at)
        object.__setattr__(
            self,
            "provider_status_observations",
            _status_observations(self.provider_status_observations, self.captured_at),
        )
        _validate_week_context(self)
        points, stats = _validated_values(
            self.status,
            self.projected_fantasy_points,
            self.raw_projected_stats,
        )
        object.__setattr__(self, "projected_fantasy_points", points)
        object.__setattr__(self, "raw_projected_stats", stats)


@dataclass(frozen=True)
class RemainingSeasonProjection:
    """A provider's ROS total, distinct from its individual weekly records."""

    canonical_player_id: str | None
    snapshot_id: str
    scoring_profile_id: str
    provider: str
    provider_player_id: str
    season: int
    applicable_weeks: tuple[int, ...]
    status: ProjectionStatus
    origin: RemainingSeasonOrigin
    captured_at: datetime
    projected_fantasy_points: float | None = None
    raw_projected_stats: Mapping[str, float] = field(default_factory=dict)
    source_published_at: datetime | None = None
    provider_status_observations: tuple[ProviderStatusObservation, ...] = ()

    def __post_init__(self) -> None:
        _validate_identity(self)
        if not isinstance(self.origin, RemainingSeasonOrigin):
            raise ValueError("origin must be a RemainingSeasonOrigin")
        allowed_statuses = {
            ProjectionStatus.OBSERVED,
            ProjectionStatus.NOT_APPLICABLE,
            ProjectionStatus.NOT_PUBLISHED,
            ProjectionStatus.PARSE_ERROR,
            ProjectionStatus.UNMATCHED_PLAYER,
        }
        if self.status not in allowed_statuses:
            raise ValueError(f"{self.status.value} is not valid for a remaining-season projection")
        _require_int("season", self.season, minimum=2012, maximum=None)
        _validate_times(self.source_published_at, self.captured_at)
        object.__setattr__(
            self,
            "provider_status_observations",
            _status_observations(self.provider_status_observations, self.captured_at),
        )
        weeks = _normalized_weeks(
            self.applicable_weeks,
            allow_empty=self.status is ProjectionStatus.NOT_APPLICABLE,
        )
        if self.status is ProjectionStatus.NOT_APPLICABLE and weeks:
            raise ValueError("not-applicable remaining-season projection cannot have applicable weeks")
        object.__setattr__(
            self,
            "applicable_weeks",
            weeks,
        )
        points, stats = _validated_values(
            self.status,
            self.projected_fantasy_points,
            self.raw_projected_stats,
        )
        object.__setattr__(self, "projected_fantasy_points", points)
        object.__setattr__(self, "raw_projected_stats", stats)


def derive_remaining_season(
    weekly_projections: Iterable[WeeklyProjection],
    applicable_weeks: Iterable[int],
) -> RemainingSeasonProjection:
    """Sum complete weekly rows; never impute an unavailable row as zero."""

    rows = tuple(weekly_projections)
    if not rows:
        raise ValueError("weekly_projections cannot be empty")
    if any(not isinstance(row, WeeklyProjection) for row in rows):
        raise ValueError("weekly_projections must contain WeeklyProjection rows")

    identity = _identity(rows[0])
    if any(_identity(row) != identity for row in rows[1:]):
        raise ValueError(
            "weekly projections must share player, provider, season, snapshot, and scoring profile"
        )

    rows_by_week: dict[int, WeeklyProjection] = {}
    for row in rows:
        if row.week in rows_by_week:
            raise ValueError(f"duplicate weekly projection for week {row.week}")
        rows_by_week[row.week] = row

    weeks = _normalized_weeks(applicable_weeks)
    selected: list[WeeklyProjection] = []
    for week in weeks:
        row = rows_by_week.get(week)
        if row is None:
            raise ValueError(f"missing projection for applicable week {week}")
        if row.status is not ProjectionStatus.OBSERVED:
            raise ValueError(f"projection for applicable week {week} must be observed")
        selected.append(row)

    stat_names = set(selected[0].raw_projected_stats)
    if any(set(row.raw_projected_stats) != stat_names for row in selected[1:]):
        raise ValueError("applicable weeks must have the same raw projected stat fields")
    aggregated_stats = {
        name: math.fsum(row.raw_projected_stats[name] for row in selected)
        for name in sorted(stat_names)
    }
    published_times = tuple(row.source_published_at for row in selected)
    published_at = (
        max(published_times) if all(value is not None for value in published_times) else None
    )

    first = rows[0]
    return RemainingSeasonProjection(
        canonical_player_id=first.canonical_player_id,
        snapshot_id=first.snapshot_id,
        scoring_profile_id=first.scoring_profile_id,
        provider=first.provider,
        provider_player_id=first.provider_player_id,
        season=first.season,
        applicable_weeks=weeks,
        status=ProjectionStatus.OBSERVED,
        origin=RemainingSeasonOrigin.DERIVED_WEEKLY,
        captured_at=max(row.captured_at for row in selected),
        projected_fantasy_points=math.fsum(
            row.projected_fantasy_points for row in selected
        ),
        raw_projected_stats=aggregated_stats,
        source_published_at=published_at,
        provider_status_observations=_merged_status_observations(selected),
    )


def _validate_identity(projection: WeeklyProjection | RemainingSeasonProjection) -> None:
    if not isinstance(projection.status, ProjectionStatus):
        raise ValueError("status must be a ProjectionStatus")
    _require_nonempty_string("provider", projection.provider)
    _require_nonempty_string("provider_player_id", projection.provider_player_id)
    _require_nonempty_string("snapshot_id", projection.snapshot_id)
    _require_nonempty_string("scoring_profile_id", projection.scoring_profile_id)
    if projection.status is ProjectionStatus.UNMATCHED_PLAYER:
        if projection.canonical_player_id is not None:
            raise ValueError("canonical_player_id must be absent for an unmatched player")
    else:
        _require_nonempty_string("canonical_player_id", projection.canonical_player_id)


def _identity(projection: WeeklyProjection) -> tuple[str | None, str, str, str, str, int]:
    return (
        projection.canonical_player_id,
        projection.snapshot_id,
        projection.scoring_profile_id,
        projection.provider,
        projection.provider_player_id,
        projection.season,
    )


def _validated_values(
    status: ProjectionStatus,
    points: float | None,
    raw_stats: Mapping[str, float],
) -> tuple[float | None, Mapping[str, float]]:
    stats = _frozen_stats(raw_stats)
    if status is ProjectionStatus.OBSERVED:
        if not _is_finite_number(points):
            raise ValueError("observed projection requires finite projected_fantasy_points")
        return float(points), stats
    if points is not None:
        raise ValueError("projected_fantasy_points must be absent unless status is observed")
    if stats:
        raise ValueError("raw_projected_stats must be empty unless status is observed")
    return None, stats


def _frozen_stats(raw_stats: Mapping[str, float]) -> Mapping[str, float]:
    if not isinstance(raw_stats, Mapping):
        raise ValueError("raw_projected_stats must be a mapping")
    copied: dict[str, float] = {}
    for name, value in raw_stats.items():
        _require_nonempty_string("raw projected stat name", name)
        if not _is_finite_number(value):
            raise ValueError(f"raw projected stat {name!r} must be a finite number")
        copied[name] = float(value)
    return _FrozenStats(sorted(copied.items()))


def _validate_week_context(projection: WeeklyProjection) -> None:
    for name in ("nfl_team_id", "nfl_game_id", "opponent_team_id"):
        value = getattr(projection, name)
        if value is not None:
            _require_nonempty_string(name, value)
    if projection.is_home is not None and not isinstance(projection.is_home, bool):
        raise ValueError("is_home must be a boolean or None")

    game_context = (
        projection.nfl_game_id,
        projection.opponent_team_id,
        projection.is_home,
    )
    if any(value is not None for value in game_context) and not all(
        value is not None for value in game_context
    ):
        raise ValueError("NFL game context requires game, opponent, and home/away fields")
    if any(value is not None for value in game_context) and projection.nfl_team_id is None:
        raise ValueError("NFL game context requires nfl_team_id")
    if projection.status is ProjectionStatus.BYE:
        if projection.nfl_team_id is None:
            raise ValueError("bye projection requires nfl_team_id")
        if any(value is not None for value in game_context):
            raise ValueError("bye projection cannot have NFL game context")
    if (
        projection.nfl_team_id is not None
        and projection.opponent_team_id == projection.nfl_team_id
    ):
        raise ValueError("opponent_team_id cannot equal nfl_team_id")


def _validate_times(published_at: datetime | None, captured_at: datetime) -> None:
    _require_aware_datetime("captured_at", captured_at)
    if published_at is not None:
        _require_aware_datetime("source_published_at", published_at)
        if published_at > captured_at:
            raise ValueError("source_published_at cannot be after captured_at")


def _status_observations(values, captured_at):
    if isinstance(values, (str, bytes)):
        raise ValueError("provider_status_observations must be an iterable")
    try:
        observations = tuple(values)
    except TypeError:
        raise ValueError("provider_status_observations must be an iterable") from None
    if any(not isinstance(row, ProviderStatusObservation) for row in observations):
        raise ValueError(
            "provider_status_observations must contain ProviderStatusObservation values"
        )
    if len(set(observations)) != len(observations):
        raise ValueError("provider_status_observations cannot contain duplicates")
    if any(row.captured_at > captured_at for row in observations):
        raise ValueError("provider status observation cannot be newer than its projection")
    return tuple(
        sorted(
            observations,
            key=lambda row: (
                row.captured_at,
                row.source_scope.value,
                row.source_week or 0,
                row.designation.casefold(),
                row.designation,
            ),
        )
    )


def _merged_status_observations(rows):
    return tuple(
        dict.fromkeys(
            observation
            for row in rows
            for observation in row.provider_status_observations
        )
    )


def _validate_designation(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("provider status designation must be a non-empty string")
    if value != " ".join(value.split()) or len(value) > 80:
        raise ValueError(
            "provider status designation must be normalized and at most 80 characters"
        )
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("provider status designation cannot contain control characters")
    if re.search(r"(?:https?://|www\.)", value, flags=re.IGNORECASE):
        raise ValueError("provider status designation cannot contain a URL")


def _normalized_weeks(
    weeks: Iterable[int],
    *,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    values = tuple(weeks)
    if not values and not allow_empty:
        raise ValueError("applicable_weeks cannot be empty")
    for week in values:
        _require_int("applicable week", week, minimum=1, maximum=25)
    if len(set(values)) != len(values):
        raise ValueError("applicable_weeks cannot contain duplicates")
    return tuple(sorted(values))


def _require_nonempty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_int(name: str, value: object, *, minimum: int, maximum: int | None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        upper = f" through {maximum}" if maximum is not None else " or later"
        raise ValueError(f"{name} must be an integer from {minimum}{upper}")


def _require_aware_datetime(name: str, value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
