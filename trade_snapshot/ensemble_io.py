"""Strict JSON records for fused weekly projections."""

from collections.abc import Mapping

from .ensemble import EnsembleProjection, ProviderObservation
from .projections import ProjectionStatus


_RECORD_KEYS = {
    "kind",
    "schema_version",
    "canonical_player_id",
    "snapshot_id",
    "scoring_profile_id",
    "season",
    "week",
    "position",
    "status",
    "provider_observations",
    "minimum_observed_sources",
    "position_stddev_floor",
    "projected_fantasy_points",
    "between_provider_stddev",
    "predictive_stddev",
    "nfl_team_id",
    "nfl_game_id",
    "opponent_team_id",
    "is_home",
}
_OBSERVATION_KEYS = {
    "provider",
    "provider_player_id",
    "status",
    "projected_fantasy_points",
    "weight",
}


def ensemble_to_record(projection: EnsembleProjection) -> dict[str, object]:
    """Return a lossless JSON-safe record for one ensemble projection."""

    if not isinstance(projection, EnsembleProjection):
        raise ValueError("projection must be an EnsembleProjection")
    return {
        "kind": "weekly_ensemble",
        "schema_version": 1,
        "canonical_player_id": projection.canonical_player_id,
        "snapshot_id": projection.snapshot_id,
        "scoring_profile_id": projection.scoring_profile_id,
        "season": projection.season,
        "week": projection.week,
        "position": projection.position,
        "status": projection.status.value,
        "provider_observations": [
            {
                "provider": item.provider,
                "provider_player_id": item.provider_player_id,
                "status": item.status.value,
                "projected_fantasy_points": item.projected_fantasy_points,
                "weight": item.weight,
            }
            for item in projection.provider_observations
        ],
        "minimum_observed_sources": projection.minimum_observed_sources,
        "position_stddev_floor": projection.position_stddev_floor,
        "projected_fantasy_points": projection.projected_fantasy_points,
        "between_provider_stddev": projection.between_provider_stddev,
        "predictive_stddev": projection.predictive_stddev,
        "nfl_team_id": projection.nfl_team_id,
        "nfl_game_id": projection.nfl_game_id,
        "opponent_team_id": projection.opponent_team_id,
        "is_home": projection.is_home,
    }


def ensemble_from_record(record: Mapping[str, object]) -> EnsembleProjection:
    """Rebuild a validated ensemble and reject unknown or inconsistent fields."""

    if not isinstance(record, Mapping) or set(record) != _RECORD_KEYS:
        raise ValueError("ensemble record fields are invalid")
    if (
        record["kind"] != "weekly_ensemble"
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
    ):
        raise ValueError("ensemble record kind or schema version is invalid")
    raw_observations = record["provider_observations"]
    if not isinstance(raw_observations, list):
        raise ValueError("provider_observations must be a JSON array")
    observations = tuple(_observation_from_record(item) for item in raw_observations)
    try:
        status = ProjectionStatus(record["status"])
    except (TypeError, ValueError):
        raise ValueError("ensemble record status is invalid") from None
    return EnsembleProjection(
        canonical_player_id=record["canonical_player_id"],
        snapshot_id=record["snapshot_id"],
        scoring_profile_id=record["scoring_profile_id"],
        season=record["season"],
        week=record["week"],
        position=record["position"],
        status=status,
        provider_observations=observations,
        minimum_observed_sources=record["minimum_observed_sources"],
        position_stddev_floor=record["position_stddev_floor"],
        projected_fantasy_points=record["projected_fantasy_points"],
        between_provider_stddev=record["between_provider_stddev"],
        predictive_stddev=record["predictive_stddev"],
        nfl_team_id=record["nfl_team_id"],
        nfl_game_id=record["nfl_game_id"],
        opponent_team_id=record["opponent_team_id"],
        is_home=record["is_home"],
    )


def _observation_from_record(record: object) -> ProviderObservation:
    if not isinstance(record, Mapping) or set(record) != _OBSERVATION_KEYS:
        raise ValueError("provider observation record fields are invalid")
    try:
        status = ProjectionStatus(record["status"])
    except (TypeError, ValueError):
        raise ValueError("provider observation status is invalid") from None
    return ProviderObservation(
        provider=record["provider"],
        provider_player_id=record["provider_player_id"],
        status=status,
        projected_fantasy_points=record["projected_fantasy_points"],
        weight=record["weight"],
    )
