"""Deterministic ECR and multi-provider projection features for calibration."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from math import log, sqrt
import re

from ._calibration_inputs import PlayerFeatureVector
from ._scenario_random import content_id
from .ecr import EcrPeriod, EcrPlayerRanking, EcrSnapshot
from .ensemble import EnsembleProjection
from .projections import ProjectionStatus
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
    "observed_week_fraction",
    "remaining_points",
)
ENSEMBLE_METRICS = (
    "current_available",
    "current_points",
    "observed_week_fraction",
    "remaining_mean",
    "remaining_points",
    "remaining_uncertainty",
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


def build_strength_features(
    ecr_snapshots: Iterable[EcrSnapshot],
    projections: Iterable[EnsembleProjection],
    eligibilities: Iterable[PlayerEligibility],
    *,
    provider_names: Iterable[str] = DEFAULT_PROVIDERS,
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
    weekly_ranks = {row.canonical_player_id: row for row in weekly.rankings}
    ros_ranks = {row.canonical_player_id: row for row in ros.rankings}
    weekly_total = _ecr_total(weekly)
    ros_total = _ecr_total(ros)
    vectors = []
    for player_id in sorted(eligibility):
        player_projections = tuple(projection_rows[(player_id, week)] for week in weeks)
        values = {"presence": 1.0}
        values.update(
            _ecr_features("ecr_weekly", weekly_ranks.get(player_id), weekly_total)
        )
        values.update(_ecr_features("ecr_ros", ros_ranks.get(player_id), ros_total))
        values.update(_projection_features(player_projections, providers))
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
    provider_set = frozenset(providers)
    grid = {}
    for row in rows:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("projection identity does not match ECR snapshots")
        if row.week < as_of_week:
            raise ValueError("projections cannot include an elapsed week")
        observed_providers = tuple(item.provider for item in row.provider_observations)
        missing = provider_set.difference(observed_providers)
        if missing:
            raise ValueError(
                f"projection is missing explicit provider evidence for {min(missing)!r}"
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


def _ecr_total(snapshot: EcrSnapshot) -> int:
    return max(len(snapshot.rankings), max(row.rank_ecr for row in snapshot.rankings))


def _ecr_features(prefix: str, row: EcrPlayerRanking | None, total: int):
    metrics = {metric: 0.0 for metric in ECR_METRICS}
    if row is None:
        return {f"{prefix}_{name}": value for name, value in metrics.items()}
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


def _projection_features(rows, providers):
    result = {}
    observed_counts = dict.fromkeys(providers, 0)
    remaining_points = dict.fromkeys(providers, 0.0)
    current_by_provider = {
        item.provider: item for item in rows[0].provider_observations
    }
    ensemble_count = 0
    ensemble_points = 0.0
    ensemble_uncertainty_squared = 0.0
    for row in rows:
        for item in row.provider_observations:
            if (
                item.provider in observed_counts
                and item.status is ProjectionStatus.OBSERVED
            ):
                observed_counts[item.provider] += 1
                remaining_points[item.provider] += item.projected_fantasy_points
        if row.status is ProjectionStatus.OBSERVED:
            ensemble_count += 1
            ensemble_points += row.projected_fantasy_points
            ensemble_uncertainty_squared += row.predictive_stddev**2

    for provider in providers:
        current = current_by_provider[provider]
        prefix = f"projection_{provider}"
        result.update(
            {
                f"{prefix}_current_available": float(
                    current.status is ProjectionStatus.OBSERVED
                ),
                f"{prefix}_current_points": (
                    current.projected_fantasy_points or 0.0
                ),
                f"{prefix}_observed_week_fraction": observed_counts[provider]
                / len(rows),
                f"{prefix}_remaining_points": remaining_points[provider],
            }
        )
    current = rows[0]
    result.update(
        {
            "projection_ensemble_current_available": float(
                current.status is ProjectionStatus.OBSERVED
            ),
            "projection_ensemble_current_points": current.projected_fantasy_points or 0.0,
            "projection_ensemble_observed_week_fraction": ensemble_count / len(rows),
            "projection_ensemble_remaining_mean": ensemble_points / len(rows),
            "projection_ensemble_remaining_points": ensemble_points,
            "projection_ensemble_remaining_uncertainty": sqrt(
                ensemble_uncertainty_squared
            ),
        }
    )
    return result
