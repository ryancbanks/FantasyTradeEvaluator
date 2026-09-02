"""Strict provider ensemble for one canonical player and NFL week."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ._ensemble_math import weighted_metrics
from ._ensemble_validation import (
    finite_float,
    freeze_floors,
    normalize_position,
    require_int,
    require_nonempty_string,
    validate_game_context,
)
from .projection_provider_rules import (
    validate_no_composite_double_count,
    validate_selectable_projection_provider,
    validate_selectable_projection_providers,
)
from .projections import ProjectionStatus, WeeklyProjection


__all__ = (
    "EnsembleConfig",
    "EnsembleProjection",
    "ProviderObservation",
    "ProviderWeight",
    "ensemble_from_record",
    "ensemble_to_record",
    "fuse_weekly_projections",
)


@dataclass(frozen=True, slots=True)
class ProviderWeight:
    """A positive weight for one required projection provider."""

    provider: str
    weight: float

    def __post_init__(self) -> None:
        provider = validate_selectable_projection_provider(self.provider)
        weight = finite_float("provider weight", self.weight)
        if weight <= 0:
            raise ValueError("provider weight must be a positive finite number")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "weight", weight)


@dataclass(frozen=True, slots=True)
class EnsembleConfig:
    """Required providers, observation quorum, and positional uncertainty floors."""

    provider_weights: tuple[ProviderWeight, ...]
    minimum_observed_sources: int
    position_stddev_floors: Mapping[str, float] = field(hash=False)

    def __post_init__(self) -> None:
        try:
            weights = tuple(self.provider_weights)
        except TypeError:
            raise ValueError("provider_weights must be an iterable") from None
        if not weights or any(not isinstance(item, ProviderWeight) for item in weights):
            raise ValueError("provider_weights must contain ProviderWeight values")
        providers = tuple(item.provider for item in weights)
        validate_selectable_projection_providers(providers)
        validate_no_composite_double_count(providers)
        if (
            isinstance(self.minimum_observed_sources, bool)
            or not isinstance(self.minimum_observed_sources, int)
            or not 1 <= self.minimum_observed_sources <= len(weights)
        ):
            raise ValueError(
                "minimum_observed_sources must be between 1 and the provider count"
            )
        floors = freeze_floors(self.position_stddev_floors)
        object.__setattr__(self, "provider_weights", weights)
        object.__setattr__(self, "position_stddev_floors", floors)

    def __hash__(self) -> int:
        return hash(
            (
                self.provider_weights,
                self.minimum_observed_sources,
                tuple(self.position_stddev_floors.items()),
            )
        )


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """The value/status retained from one configured provider row."""

    provider: str
    provider_player_id: str
    status: ProjectionStatus
    projected_fantasy_points: float | None
    weight: float

    def __post_init__(self) -> None:
        provider = validate_selectable_projection_provider(self.provider)
        require_nonempty_string("provider_player_id", self.provider_player_id)
        if not isinstance(self.status, ProjectionStatus):
            raise ValueError("provider observation status must be a ProjectionStatus")
        weight = finite_float("provider observation weight", self.weight)
        if weight <= 0:
            raise ValueError("provider observation weight must be positive and finite")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "weight", weight)
        if self.status is ProjectionStatus.OBSERVED:
            object.__setattr__(
                self,
                "projected_fantasy_points",
                finite_float(
                    "observed provider value", self.projected_fantasy_points
                ),
            )
        elif self.projected_fantasy_points is not None:
            raise ValueError("unavailable provider value must be absent")


@dataclass(frozen=True, slots=True)
class EnsembleProjection:
    """A reproducible fused projection and its retained provider evidence.

    For observed values, provider disagreement is the population weighted
    standard deviation ``d = sqrt(sum(w*(x-mean)^2) / sum(w))`` over observed
    providers. Predictive uncertainty is ``sqrt(position_floor^2 + d^2)``.
    """

    canonical_player_id: str
    snapshot_id: str
    scoring_profile_id: str
    season: int
    week: int
    position: str
    status: ProjectionStatus
    provider_observations: tuple[ProviderObservation, ...]
    minimum_observed_sources: int
    position_stddev_floor: float
    projected_fantasy_points: float | None
    between_provider_stddev: float | None
    predictive_stddev: float | None
    nfl_team_id: str | None = None
    nfl_game_id: str | None = None
    opponent_team_id: str | None = None
    is_home: bool | None = None

    def __post_init__(self) -> None:
        for name in ("canonical_player_id", "snapshot_id", "scoring_profile_id"):
            require_nonempty_string(name, getattr(self, name))
        require_int("season", self.season, 2012, None)
        require_int("week", self.week, 1, 25)
        object.__setattr__(self, "position", normalize_position(self.position))
        if not isinstance(self.status, ProjectionStatus) or self.status not in (
            ProjectionStatus.OBSERVED,
            ProjectionStatus.BYE,
        ):
            raise ValueError("ensemble status must be observed or bye")
        validate_game_context(self)

        try:
            observations = tuple(self.provider_observations)
        except TypeError:
            raise ValueError("provider_observations must be an iterable") from None
        if not observations or any(
            not isinstance(item, ProviderObservation) for item in observations
        ):
            raise ValueError(
                "provider_observations must contain ProviderObservation values"
            )
        providers = tuple(item.provider for item in observations)
        validate_selectable_projection_providers(providers)
        validate_no_composite_double_count(providers)
        object.__setattr__(self, "provider_observations", observations)
        if (
            isinstance(self.minimum_observed_sources, bool)
            or not isinstance(self.minimum_observed_sources, int)
            or not 1 <= self.minimum_observed_sources <= len(observations)
        ):
            raise ValueError("minimum_observed_sources is invalid")
        floor = finite_float("position_stddev_floor", self.position_stddev_floor)
        if floor < 0:
            raise ValueError("position_stddev_floor must be finite and nonnegative")
        object.__setattr__(self, "position_stddev_floor", floor)
        self._validate_calculation()

    @property
    def observed_source_count(self) -> int:
        return sum(
            item.status is ProjectionStatus.OBSERVED
            for item in self.provider_observations
        )

    def _validate_calculation(self) -> None:
        all_bye = all(
            item.status is ProjectionStatus.BYE for item in self.provider_observations
        )
        if self.status is ProjectionStatus.BYE:
            if not all_bye:
                raise ValueError("bye ensemble requires every provider to report bye")
            if any(
                value is not None
                for value in (
                    self.projected_fantasy_points,
                    self.between_provider_stddev,
                    self.predictive_stddev,
                )
            ):
                raise ValueError("bye ensemble cannot carry numeric output")
            return
        if all_bye:
            raise ValueError("all-bye provider rows require bye ensemble status")
        if self.observed_source_count < self.minimum_observed_sources:
            raise ValueError("insufficient observed provider sources")
        mean, disagreement, predictive = weighted_metrics(
            self.provider_observations,
            self.position_stddev_floor,
        )
        supplied = (
            self.projected_fantasy_points,
            self.between_provider_stddev,
            self.predictive_stddev,
        )
        for name, actual, expected in zip(
            (
                "projected_fantasy_points",
                "between_provider_stddev",
                "predictive_stddev",
            ),
            supplied,
            (mean, disagreement, predictive),
        ):
            if finite_float(name, actual) != expected:
                raise ValueError(f"{name} does not match provider observations")
            object.__setattr__(self, name, float(actual))


def fuse_weekly_projections(
    weekly_projections: Iterable[WeeklyProjection],
    position: str,
    config: EnsembleConfig,
) -> EnsembleProjection:
    """Fuse configured rows for exactly one player/week without imputing missing data."""

    if not isinstance(config, EnsembleConfig):
        raise ValueError("config must be an EnsembleConfig")
    try:
        rows = tuple(weekly_projections)
    except TypeError:
        raise ValueError("weekly_projections must be an iterable") from None
    if not rows or any(not isinstance(row, WeeklyProjection) for row in rows):
        raise ValueError("weekly_projections must contain WeeklyProjection rows")

    rows_by_provider: dict[str, WeeklyProjection] = {}
    for row in rows:
        if row.provider in rows_by_provider:
            raise ValueError(f"duplicate provider row: {row.provider}")
        rows_by_provider[row.provider] = row
    configured = tuple(item.provider for item in config.provider_weights)
    validate_selectable_projection_providers(configured)
    validate_no_composite_double_count(configured)
    missing = [provider for provider in configured if provider not in rows_by_provider]
    if missing:
        raise ValueError(f"missing required provider: {missing[0]}")
    extras = [provider for provider in rows_by_provider if provider not in configured]
    if extras:
        raise ValueError(f"unconfigured provider: {extras[0]}")

    context = _row_context(rows[0])
    if any(_row_context(row) != context for row in rows[1:]):
        raise ValueError("weekly projections must share identity and game context")
    position_key = normalize_position(position)
    try:
        floor = config.position_stddev_floors[position_key]
    except KeyError:
        raise ValueError(
            f"no predictive uncertainty floor configured for position {position_key}"
        ) from None

    weight_by_provider = {
        item.provider: item.weight for item in config.provider_weights
    }
    observations = tuple(
        ProviderObservation(
            provider=provider,
            provider_player_id=rows_by_provider[provider].provider_player_id,
            status=rows_by_provider[provider].status,
            projected_fantasy_points=rows_by_provider[
                provider
            ].projected_fantasy_points,
            weight=weight_by_provider[provider],
        )
        for provider in configured
    )
    all_bye = all(item.status is ProjectionStatus.BYE for item in observations)
    observed_count = sum(
        item.status is ProjectionStatus.OBSERVED for item in observations
    )
    if not all_bye and observed_count < config.minimum_observed_sources:
        raise ValueError(
            f"insufficient observed provider sources: {observed_count} available, "
            f"{config.minimum_observed_sources} required"
        )
    mean = disagreement = predictive = None
    status = ProjectionStatus.BYE if all_bye else ProjectionStatus.OBSERVED
    if status is ProjectionStatus.OBSERVED:
        mean, disagreement, predictive = weighted_metrics(observations, floor)

    first = rows[0]
    return EnsembleProjection(
        canonical_player_id=first.canonical_player_id,
        snapshot_id=first.snapshot_id,
        scoring_profile_id=first.scoring_profile_id,
        season=first.season,
        week=first.week,
        position=position_key,
        status=status,
        provider_observations=observations,
        minimum_observed_sources=config.minimum_observed_sources,
        position_stddev_floor=floor,
        projected_fantasy_points=mean,
        between_provider_stddev=disagreement,
        predictive_stddev=predictive,
        nfl_team_id=first.nfl_team_id,
        nfl_game_id=first.nfl_game_id,
        opponent_team_id=first.opponent_team_id,
        is_home=first.is_home,
    )


def ensemble_to_record(projection: EnsembleProjection) -> dict[str, object]:
    """Return a lossless JSON-safe record for one ensemble projection."""

    from .ensemble_io import ensemble_to_record as serialize

    return serialize(projection)


def ensemble_from_record(record: Mapping[str, object]) -> EnsembleProjection:
    """Rebuild a validated ensemble and reject unknown or inconsistent fields."""

    from .ensemble_io import ensemble_from_record as deserialize

    return deserialize(record)


def _row_context(row: WeeklyProjection) -> tuple[object, ...]:
    return (
        row.canonical_player_id,
        row.snapshot_id,
        row.scoring_profile_id,
        row.season,
        row.week,
        row.nfl_team_id,
        row.nfl_game_id,
        row.opponent_team_id,
        row.is_home,
    )
