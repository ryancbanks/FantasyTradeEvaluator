"""Deterministic ECR and multi-provider projection features for calibration."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from math import fsum, log, sqrt
import re

from ._calibration_inputs import PlayerFeatureVector
from ._scenario_random import content_id
from .ecr import EcrPeriod, EcrPlayerRanking, EcrSnapshot
from .ensemble import EnsembleProjection, ProviderObservation
from .projection_lineage import ProjectionLineageIndex
from .remaining_projection import summarize_remaining_projection
from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjection,
)
from .scenario_config import PlayerEligibility


_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_PROVIDERS = ("fantasypros", "espn", "yahoo")
ECR_METRICS = (
    "available",
    "confidence",
    "inverse_rank",
    "inverse_sqrt_rank",
    "log_strength",
    "percentile",
    "position_inverse_rank",
    "range_confidence",
)
PROJECTION_METRICS = (
    "current_available",
    "current_points",
    "full_ros_available",
    "observed_week_fraction",
    "full_ros_points",
)
ENSEMBLE_METRICS = (
    "current_available",
    "current_points",
    "full_ros_available",
    "observed_week_fraction",
    "full_ros_mean",
    "full_ros_points",
    "regular_season_uncertainty",
)


@dataclass(frozen=True, slots=True)
class StrengthFeatureSet:
    snapshot_id: str
    scoring_profile_id: str
    season: int
    as_of_week: int
    weeks: tuple[int, ...]
    provider_names: tuple[str, ...]
    ecr_ids: tuple[str, str]
    player_features: tuple[PlayerFeatureVector, ...]
    feature_set_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be a non-empty string")
        if not isinstance(self.scoring_profile_id, str) or not self.scoring_profile_id.strip():
            raise ValueError("scoring_profile_id must be a non-empty string")
        if type(self.season) is not int or self.season < 2012:
            raise ValueError("season must be an integer of at least 2012")
        if type(self.as_of_week) is not int or not 1 <= self.as_of_week <= 25:
            raise ValueError("as_of_week must be an integer between 1 and 25")
        weeks = tuple(self.weeks)
        if (
            not weeks
            or weeks[0] != self.as_of_week
            or any(type(week) is not int or not 1 <= week <= 25 for week in weeks)
            or any(left >= right for left, right in zip(weeks, weeks[1:]))
        ):
            raise ValueError("weeks must be increasing and begin at as_of_week")
        providers = _providers(self.provider_names)
        ecr_ids = tuple(self.ecr_ids)
        if len(ecr_ids) != 2 or any(
            not isinstance(value, str) or not value for value in ecr_ids
        ):
            raise ValueError("ecr_ids must contain weekly and ROS content IDs")
        rows = tuple(self.player_features)
        if not rows or any(not isinstance(row, PlayerFeatureVector) for row in rows):
            raise ValueError("player_features must contain PlayerFeatureVector values")
        ids = tuple(row.player_id for row in rows)
        if len(set(ids)) != len(ids) or ids != tuple(sorted(ids)):
            raise ValueError("player_features must be unique and sorted by player_id")
        names = tuple(rows[0].values)
        if any(tuple(row.values) != names for row in rows):
            raise ValueError("every player feature row must have the same feature names")
        if names != feature_names(providers):
            raise ValueError("player feature names do not match provider_names")
        object.__setattr__(self, "weeks", weeks)
        object.__setattr__(self, "provider_names", providers)
        object.__setattr__(self, "ecr_ids", ecr_ids)
        object.__setattr__(self, "player_features", rows)
        object.__setattr__(
            self,
            "feature_set_id",
            content_id(
                "features",
                {
                    "as_of_week": self.as_of_week,
                    "ecr_ids": list(self.ecr_ids),
                    "players": [
                        {
                            "eligible_positions": sorted(row.eligible_positions),
                            "player_id": row.player_id,
                            "values": dict(row.values),
                        }
                        for row in rows
                    ],
                    "provider_names": list(self.provider_names),
                    "scoring_profile_id": self.scoring_profile_id,
                    "season": self.season,
                    "snapshot_id": self.snapshot_id,
                    "weeks": list(self.weeks),
                },
            ),
        )

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(self.player_features[0].values)


@dataclass(frozen=True, slots=True)
class ProjectionAvailabilityRequirements:
    """Projection horizons whose numeric values an active formula consumes."""

    current_providers: frozenset[str]
    full_ros_providers: frozenset[str]
    ensemble_current: bool
    ensemble_full_ros: bool


def build_strength_features(
    ecr_snapshots: Iterable[EcrSnapshot],
    projections: Iterable[EnsembleProjection],
    eligibilities: Iterable[PlayerEligibility],
    *,
    provider_names: Iterable[str] = DEFAULT_PROVIDERS,
    projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    remaining_week_scopes: Mapping[str, Iterable[int]],
) -> StrengthFeatureSet:
    weekly, ros = _ecr_pair(ecr_snapshots)
    providers = _providers(provider_names)
    eligibility = _eligibility(eligibilities)
    projection_rows, weeks = _projection_grid(
        projections,
        player_ids=set(eligibility),
        providers=providers,
        identity=(weekly.snapshot_id, weekly.scoring_profile_id, weekly.season),
        as_of_week=weekly.as_of_week,
    )
    lineage = ProjectionLineageIndex(
        projection_rows.values(),
        projection_evidence,
    )
    remaining_scopes = _remaining_scopes(
        remaining_week_scopes,
        player_ids=set(eligibility),
        first_week=weekly.as_of_week,
    )
    weekly_ranks = {row.canonical_player_id: row for row in weekly.rankings}
    ros_ranks = {row.canonical_player_id: row for row in ros.rankings}
    vectors = []
    for player_id in sorted(eligibility):
        player_projections = tuple(projection_rows[(player_id, week)] for week in weeks)
        values = {"presence": 1.0}
        values.update(_ecr_features("ecr_weekly", weekly_ranks.get(player_id), weekly))
        values.update(_ecr_features("ecr_ros", ros_ranks.get(player_id), ros))
        values.update(
            _projection_features(
                player_projections,
                providers,
                lineage=lineage,
                remaining_scope=remaining_scopes.get(player_id),
            )
        )
        vectors.append(
            PlayerFeatureVector(
                player_id,
                frozenset(eligibility[player_id].eligible_slots),
                values,
            )
        )
    return StrengthFeatureSet(
        snapshot_id=weekly.snapshot_id,
        scoring_profile_id=weekly.scoring_profile_id,
        season=weekly.season,
        as_of_week=weekly.as_of_week,
        weeks=weeks,
        provider_names=providers,
        ecr_ids=(weekly.ecr_id, ros.ecr_id),
        player_features=tuple(vectors),
    )


def feature_names(provider_names: Iterable[str] = DEFAULT_PROVIDERS) -> tuple[str, ...]:
    providers = _providers(provider_names)
    names = ["presence"]
    for prefix in ("ecr_weekly", "ecr_ros"):
        names.extend(f"{prefix}_{metric}" for metric in ECR_METRICS)
    for provider in providers:
        names.extend(f"projection_{provider}_{metric}" for metric in PROJECTION_METRICS)
    names.extend(f"projection_ensemble_{metric}" for metric in ENSEMBLE_METRICS)
    return tuple(sorted(names))


def require_available_features(
    features: StrengthFeatureSet,
    required_feature_names: Iterable[str],
) -> None:
    """Fail when a numeric formula input has an explicit unavailable companion."""

    if not isinstance(features, StrengthFeatureSet):
        raise ValueError("features must be a StrengthFeatureSet")
    required = _required_feature_names(required_feature_names)
    for name in sorted(set(required)):
        availability = _availability_feature(name)
        if availability is None or availability not in features.feature_names:
            continue
        unavailable = tuple(
            row.player_id
            for row in features.player_features
            if row.values[availability] != 1.0
        )
        if unavailable:
            raise ValueError(
                f"required feature {name!r} is unavailable for player "
                f"{unavailable[0]!r}"
            )


def projection_availability_requirements(
    required_feature_names: Iterable[str],
    provider_names: Iterable[str],
) -> ProjectionAvailabilityRequirements:
    """Preserve the source and horizon of every required projection value."""

    required = _required_feature_names(required_feature_names)
    providers = _providers(provider_names)
    current = {
        f"projection_{provider}_current_points": provider
        for provider in providers
    }
    full_ros = {
        f"projection_{provider}_full_ros_points": provider
        for provider in providers
    }
    current_providers = set()
    full_ros_providers = set()
    ensemble_current = False
    ensemble_full_ros = False
    for name in required:
        provider = current.get(name)
        if provider is not None:
            current_providers.add(provider)
            continue
        provider = full_ros.get(name)
        if provider is not None:
            full_ros_providers.add(provider)
            continue
        if name == "projection_ensemble_current_points":
            ensemble_current = True
            continue
        if name in {
            "projection_ensemble_full_ros_mean",
            "projection_ensemble_full_ros_points",
        }:
            ensemble_full_ros = True
            continue
        if (
            name.startswith("projection_")
            and _availability_feature(name) is not None
        ):
            raise ValueError(
                f"required projection feature {name!r} has no configured provider "
                "or supported numeric metric"
            )
    return ProjectionAvailabilityRequirements(
        frozenset(current_providers),
        frozenset(full_ros_providers),
        ensemble_current,
        ensemble_full_ros,
    )


def _required_feature_names(values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("required_feature_names must be an iterable")
    try:
        result = tuple(values)
    except TypeError:
        raise ValueError("required_feature_names must be an iterable") from None
    if any(not isinstance(name, str) or not name for name in result):
        raise ValueError("required_feature_names must contain non-empty strings")
    return result


def _availability_feature(name: str) -> str | None:
    for prefix in ("ecr_weekly_", "ecr_ros_"):
        if name.startswith(prefix) and name != f"{prefix}available":
            return f"{prefix}available"
    if name.startswith("projection_") and name.endswith("_current_points"):
        return name.removesuffix("_current_points") + "_current_available"
    if name.startswith("projection_") and (
        name.endswith("_full_ros_points") or name.endswith("_full_ros_mean")
    ):
        return name.rsplit("_full_ros_", 1)[0] + "_full_ros_available"
    return None


def _ecr_pair(values) -> tuple[EcrSnapshot, EcrSnapshot]:
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("ecr_snapshots must be an iterable") from None
    if len(rows) != 2 or any(not isinstance(row, EcrSnapshot) for row in rows):
        raise ValueError("ecr_snapshots must contain one weekly and one ROS snapshot")
    by_period = {row.period: row for row in rows}
    if set(by_period) != {EcrPeriod.WEEKLY, EcrPeriod.REST_OF_SEASON}:
        raise ValueError("ecr_snapshots must contain one weekly and one ROS snapshot")
    weekly, ros = by_period[EcrPeriod.WEEKLY], by_period[EcrPeriod.REST_OF_SEASON]
    identity = (weekly.snapshot_id, weekly.scoring_profile_id, weekly.season, weekly.as_of_week)
    if (ros.snapshot_id, ros.scoring_profile_id, ros.season, ros.as_of_week) != identity:
        raise ValueError("weekly and ROS ECR snapshots do not share one identity")
    return weekly, ros


def _providers(values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("provider_names must be an iterable")
    try:
        providers = tuple(values)
    except TypeError:
        raise ValueError("provider_names must be an iterable") from None
    if not providers or any(
        not isinstance(value, str) or not _PROVIDER_NAME.fullmatch(value)
        for value in providers
    ):
        raise ValueError("provider_names must be lowercase identifier strings")
    if len(set(providers)) != len(providers):
        raise ValueError("provider_names contains a duplicate")
    return tuple(sorted(providers))


def _eligibility(values) -> dict[str, PlayerEligibility]:
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("eligibilities must be an iterable") from None
    if not rows or any(not isinstance(row, PlayerEligibility) for row in rows):
        raise ValueError("eligibilities must contain PlayerEligibility values")
    result = {row.canonical_player_id: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("eligibilities contains a duplicate player")
    return result


def _projection_grid(values, *, player_ids, providers, identity, as_of_week):
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("projections must be an iterable") from None
    if not rows or any(not isinstance(row, EnsembleProjection) for row in rows):
        raise ValueError("projections must contain EnsembleProjection values")
    grid = {}
    for row in rows:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("projection identity does not match ECR snapshots")
        if row.week < as_of_week:
            raise ValueError("projections cannot include an elapsed week")
        observed_providers = {
            item.provider for item in row.provider_observations
        }
        missing = set(providers).difference(observed_providers)
        if missing:
            raise ValueError(
                f"projection is missing explicit provider evidence for {min(missing)!r}"
            )
        unconfigured = observed_providers.difference(providers)
        if unconfigured:
            raise ValueError(
                f"projection contains unconfigured provider {min(unconfigured)!r}"
            )
        key = (row.canonical_player_id, row.week)
        if key in grid:
            raise ValueError("projections contain a duplicate player/week")
        grid[key] = row
    if {player_id for player_id, _ in grid} != player_ids:
        raise ValueError("projection and eligibility player universes do not match")
    weeks = tuple(sorted({week for _, week in grid}))
    expected = {(player_id, week) for player_id in player_ids for week in weeks}
    if set(grid) != expected or weeks[0] != as_of_week:
        raise ValueError("projections must form a complete grid beginning at as_of_week")
    return grid, weeks


def _ecr_features(prefix: str, row: EcrPlayerRanking | None, snapshot: EcrSnapshot):
    metrics = {metric: 0.0 for metric in ECR_METRICS}
    if row is None:
        return {f"{prefix}_{name}": value for name, value in metrics.items()}
    total = max(len(snapshot.rankings), max(item.rank_ecr for item in snapshot.rankings))
    metrics.update(
        {
            "available": 1.0,
            "confidence": 1.0 / (1.0 + row.rank_stddev),
            "inverse_rank": 1.0 / row.rank_ecr,
            "inverse_sqrt_rank": 1.0 / sqrt(row.rank_ecr),
            "log_strength": log((total + 1.0) / row.rank_ecr),
            "percentile": (total - row.rank_ecr + 1.0) / total,
            "position_inverse_rank": 1.0 / row.position_rank,
            "range_confidence": 1.0 / (1.0 + row.rank_max - row.rank_min),
        }
    )
    return {f"{prefix}_{name}": value for name, value in metrics.items()}


def _projection_features(rows, providers, *, lineage, remaining_scope):
    result = {}
    full_remaining = summarize_remaining_projection(
        rows,
        lineage,
        applicable_weeks=remaining_scope,
    )
    assert full_remaining is not None
    for provider in providers:
        observations = tuple(_observation(row, provider) for row in rows)
        observed = tuple(
            item for item in observations if item.status is ProjectionStatus.OBSERVED
        )
        current = observations[0]
        full_observation = next(
            row
            for row in full_remaining.provider_observations
            if row.provider == provider
        )
        full_available = full_observation.status is ProjectionStatus.OBSERVED
        prefix = f"projection_{provider}"
        result.update(
            {
                f"{prefix}_current_available": float(
                    current.status is ProjectionStatus.OBSERVED
                ),
                f"{prefix}_current_points": (
                    current.projected_fantasy_points or 0.0
                ),
                f"{prefix}_full_ros_available": float(full_available),
                f"{prefix}_observed_week_fraction": len(observed) / len(rows),
                f"{prefix}_full_ros_points": (
                    full_observation.projected_fantasy_points or 0.0
                ),
            }
        )
    ensemble_observed = tuple(
        row for row in rows if row.status is ProjectionStatus.OBSERVED
    )
    current = rows[0]
    uncertainties = tuple(row.predictive_stddev for row in ensemble_observed)
    full_available = full_remaining.projected_fantasy_points is not None
    result.update(
        {
            "projection_ensemble_current_available": float(
                current.status is ProjectionStatus.OBSERVED
            ),
            "projection_ensemble_current_points": current.projected_fantasy_points or 0.0,
            "projection_ensemble_full_ros_available": float(full_available),
            "projection_ensemble_observed_week_fraction": len(ensemble_observed) / len(rows),
            "projection_ensemble_full_ros_mean": (
                full_remaining.average_active_week or 0.0
            ),
            "projection_ensemble_full_ros_points": (
                full_remaining.projected_fantasy_points or 0.0
            ),
            "projection_ensemble_regular_season_uncertainty": sqrt(
                fsum(value * value for value in uncertainties)
            ),
        }
    )
    return result


def _remaining_scopes(values, *, player_ids, first_week):
    if not isinstance(values, Mapping) or set(values) != player_ids:
        raise ValueError("remaining_week_scopes must exactly cover every player")
    result = {}
    for player_id, raw_weeks in values.items():
        try:
            weeks = tuple(raw_weeks)
        except TypeError:
            raise ValueError("remaining week scope must be an iterable") from None
        if (
            not weeks
            or weeks != tuple(sorted(set(weeks)))
            or any(type(week) is not int or week < first_week or week > 25 for week in weeks)
        ):
            raise ValueError(
                "remaining week scope must be unique, increasing, and not elapsed"
            )
        result[player_id] = weeks
    return result


def _observation(row: EnsembleProjection, provider: str) -> ProviderObservation:
    return next(item for item in row.provider_observations if item.provider == provider)
