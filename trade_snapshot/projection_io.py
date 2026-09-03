"""Canonical JSON records for normalized projection persistence."""

from collections.abc import Mapping
from datetime import datetime, timezone

from .projections import (
    ProjectionStatus,
    ProviderStatusObservation,
    ProviderStatusScope,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjectionOrigin,
    WeeklyProjection,
)


_COMMON_KEYS = {
    "canonical_player_id",
    "snapshot_id",
    "scoring_profile_id",
    "provider",
    "provider_player_id",
    "season",
    "status",
    "captured_at",
    "source_published_at",
    "projected_fantasy_points",
    "raw_projected_stats",
    "provider_status_observations",
    "kind",
}
_WEEKLY_KEYS = _COMMON_KEYS | {
    "origin",
    "week",
    "nfl_team_id",
    "nfl_game_id",
    "opponent_team_id",
    "is_home",
}
_ROS_KEYS = _COMMON_KEYS | {"applicable_weeks", "origin"}


def projection_to_record(
    projection: WeeklyProjection | RemainingSeasonProjection,
) -> dict[str, object]:
    """Return a canonical JSON-ready record for snapshot/cache storage."""

    if not isinstance(projection, (WeeklyProjection, RemainingSeasonProjection)):
        raise ValueError("projection must be a WeeklyProjection or RemainingSeasonProjection")
    record: dict[str, object] = {
        "canonical_player_id": projection.canonical_player_id,
        "snapshot_id": projection.snapshot_id,
        "scoring_profile_id": projection.scoring_profile_id,
        "provider": projection.provider,
        "provider_player_id": projection.provider_player_id,
        "season": projection.season,
        "status": projection.status.value,
        "captured_at": _iso_utc(projection.captured_at),
        "source_published_at": (
            _iso_utc(projection.source_published_at)
            if projection.source_published_at is not None
            else None
        ),
        "projected_fantasy_points": projection.projected_fantasy_points,
        "raw_projected_stats": dict(projection.raw_projected_stats),
        "provider_status_observations": [
            {
                "designation": observation.designation,
                "captured_at": _iso_utc(observation.captured_at),
                "source_scope": observation.source_scope.value,
                "source_week": observation.source_week,
            }
            for observation in projection.provider_status_observations
        ],
    }
    if isinstance(projection, WeeklyProjection):
        record.update(
            {
                "kind": "weekly",
                "origin": projection.origin.value,
                "week": projection.week,
                "nfl_team_id": projection.nfl_team_id,
                "nfl_game_id": projection.nfl_game_id,
                "opponent_team_id": projection.opponent_team_id,
                "is_home": projection.is_home,
            }
        )
    else:
        record.update(
            {
                "kind": "remaining_season",
                "applicable_weeks": list(projection.applicable_weeks),
                "origin": projection.origin.value,
            }
        )
    return record


def projection_from_record(record: Mapping[str, object]):
    """Rebuild a validated projection and reject unknown or missing fields."""

    if not isinstance(record, Mapping):
        raise ValueError("projection record must be a mapping")
    kind = record.get("kind")
    expected = _WEEKLY_KEYS if kind == "weekly" else _ROS_KEYS if kind == "remaining_season" else None
    if expected is None:
        raise ValueError("projection record kind must be weekly or remaining_season")
    if set(record) != expected:
        raise ValueError("projection record fields do not match its kind")

    common = {
        "canonical_player_id": record["canonical_player_id"],
        "snapshot_id": record["snapshot_id"],
        "scoring_profile_id": record["scoring_profile_id"],
        "provider": record["provider"],
        "provider_player_id": record["provider_player_id"],
        "season": record["season"],
        "status": _enum_value(ProjectionStatus, record["status"], "status"),
        "captured_at": _parse_time(record["captured_at"], "captured_at"),
        "source_published_at": _optional_time(record["source_published_at"]),
        "projected_fantasy_points": record["projected_fantasy_points"],
        "raw_projected_stats": record["raw_projected_stats"],
        "provider_status_observations": _status_observations(
            record["provider_status_observations"]
        ),
    }
    if kind == "weekly":
        return WeeklyProjection(
            **common,
            origin=_enum_value(WeeklyProjectionOrigin, record["origin"], "origin"),
            week=record["week"],
            nfl_team_id=record["nfl_team_id"],
            nfl_game_id=record["nfl_game_id"],
            opponent_team_id=record["opponent_team_id"],
            is_home=record["is_home"],
        )
    return RemainingSeasonProjection(
        **common,
        applicable_weeks=record["applicable_weeks"],
        origin=_enum_value(RemainingSeasonOrigin, record["origin"], "origin"),
    )


def _enum_value(enum_type, value: object, name: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        raise ValueError(f"projection record {name} is invalid") from None


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value, "source_published_at")


def _status_observations(value: object) -> tuple[ProviderStatusObservation, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("projection record provider_status_observations must be a list")
    try:
        records = tuple(value)
    except TypeError:
        raise ValueError(
            "projection record provider_status_observations must be a list"
        ) from None
    result = []
    expected = {"designation", "captured_at", "source_scope", "source_week"}
    for record in records:
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ValueError("projection record provider status fields are invalid")
        result.append(
            ProviderStatusObservation(
                designation=record["designation"],
                captured_at=_parse_time(
                    record["captured_at"], "provider status captured_at"
                ),
                source_scope=_enum_value(
                    ProviderStatusScope,
                    record["source_scope"],
                    "provider status source_scope",
                ),
                source_week=record["source_week"],
            )
        )
    return tuple(result)


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"projection record {name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"projection record {name} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"projection record {name} must include a timezone")
    return parsed


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )
