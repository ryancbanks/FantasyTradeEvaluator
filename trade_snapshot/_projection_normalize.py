"""Convert parsed provider rows into immutable local projection evidence."""

from datetime import datetime, timezone

from .capture_schema import GenericTableArtifact, RankingHorizon
from .identity import IdentityRegistry
from .identity_match import ProviderPlayerRecord
from .nfl_schedule import NflSchedule, NflTeamWeekStatus, canonical_nfl_game_id
from ._projection_parse import (
    ProjectionArtifactRow,
    normalize_position,
    projection_artifact_rows,
)
from .projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
)


def projection_provider_records(
    artifact: GenericTableArtifact,
    *,
    known_registry: IdentityRegistry | None = None,
) -> tuple[ProviderPlayerRecord, ...]:
    return tuple(
        ProviderPlayerRecord(
            row.identity_provider,
            row.provider_player_id,
            row.display_name,
            row.position,
            row.nfl_team_id,
        )
        for row in projection_artifact_rows(artifact, known_registry=known_registry)
    )


def projection_evidence_from_artifact(
    artifact: GenericTableArtifact,
    registry: IdentityRegistry,
    *,
    snapshot_id: str,
    scoring_profile_id: str,
    applicable_weeks: tuple[int, ...] = (),
    nfl_schedule: NflSchedule | None = None,
) -> tuple[WeeklyProjection | RemainingSeasonProjection, ...]:
    if not isinstance(registry, IdentityRegistry):
        raise ValueError("registry must be an IdentityRegistry")
    if artifact.horizon is RankingHorizon.ROS:
        weeks = tuple(applicable_weeks)
        if not weeks:
            raise ValueError("ROS projection normalization requires applicable_weeks")
    else:
        weeks = ()
    if (
        artifact.horizon is RankingHorizon.ROS
        and artifact.provider.value in {"cbs", "fftoday"}
        and not isinstance(nfl_schedule, NflSchedule)
    ):
        raise ValueError(
            f"{artifact.provider.value} full-season normalization requires the NFL schedule"
        )
    captured_at = _time(artifact.captured_at)
    result = []
    for row in projection_artifact_rows(artifact, known_registry=registry):
        identity = registry.lookup(row.identity_provider, row.provider_player_id)
        unmatched = identity is None
        if identity is not None and normalize_position(identity.position) != row.position:
            raise ValueError(
                f"projection position changed for resolved player {row.provider_player_id!r}"
            )
        points, raw_stats, ros_origin = _projected_values(
            row, artifact, weeks, nfl_schedule
        )
        status = _status(row, artifact.horizon, unmatched, points)
        common = dict(
            canonical_player_id=None if unmatched else identity.canonical_player_id,
            snapshot_id=snapshot_id,
            scoring_profile_id=scoring_profile_id,
            provider=artifact.provider.value,
            provider_player_id=row.provider_player_id,
            season=artifact.season,
            status=status,
            captured_at=captured_at,
            projected_fantasy_points=(
                points
                if status is ProjectionStatus.OBSERVED
                else None
            ),
            raw_projected_stats=(
                raw_stats
                if status is ProjectionStatus.OBSERVED
                else {}
            ),
        )
        if artifact.horizon is RankingHorizon.WEEKLY:
            result.append(_weekly(common, artifact, row, unmatched, status))
        else:
            result.append(
                RemainingSeasonProjection(
                    **common,
                    applicable_weeks=weeks,
                    origin=ros_origin,
                )
            )
    return tuple(result)


def _status(
    row: ProjectionArtifactRow,
    horizon: RankingHorizon,
    unmatched: bool,
    projected_points: float | None,
) -> ProjectionStatus:
    if unmatched:
        return ProjectionStatus.UNMATCHED_PLAYER
    if row.is_bye and horizon is RankingHorizon.WEEKLY:
        return ProjectionStatus.BYE
    if projected_points is not None:
        return ProjectionStatus.OBSERVED
    return ProjectionStatus.NOT_PUBLISHED


def _projected_values(row, artifact, weeks, nfl_schedule):
    """Return period-compatible points without treating a full season as ROS."""

    stats = dict(row.raw_projected_stats)
    if artifact.horizon is not RankingHorizon.ROS:
        return (
            row.projected_fantasy_points,
            stats,
            RemainingSeasonOrigin.PROVIDER_PUBLISHED,
        )
    if artifact.provider.value == "fftoday":
        if row.nfl_team_id == "FA":
            return None, {}, RemainingSeasonOrigin.DERIVED_FULL_SEASON
        active_games = _active_games(nfl_schedule, row.nfl_team_id, weeks)
        season_games = sum(
            team_week.nfl_team_id == row.nfl_team_id
            and team_week.status is NflTeamWeekStatus.SCHEDULED
            for team_week in nfl_schedule.team_weeks
        )
        if season_games <= 0:
            return None, {}, RemainingSeasonOrigin.DERIVED_FULL_SEASON
        fraction = active_games / season_games
        points = (
            None
            if row.projected_fantasy_points is None
            else row.projected_fantasy_points * fraction
        )
        return (
            points,
            _remaining_stats(stats, fraction, active_games),
            RemainingSeasonOrigin.DERIVED_FULL_SEASON,
        )
    if artifact.provider.value != "cbs":
        return (
            row.projected_fantasy_points,
            stats,
            RemainingSeasonOrigin.PROVIDER_PUBLISHED,
        )
    points_per_game = stats.get("fppg")
    if points_per_game is None or points_per_game < 0 or row.nfl_team_id == "FA":
        return None, {}, RemainingSeasonOrigin.DERIVED_FULL_SEASON
    if artifact.scoring == "HALF" and row.position in {"RB", "WR", "TE"}:
        games = stats.get("gp")
        receptions = stats.get("rec")
        if games is None or games <= 0 or receptions is None or receptions < 0:
            return None, {}, RemainingSeasonOrigin.DERIVED_FULL_SEASON
        points_per_game -= 0.5 * receptions / games
    if points_per_game < 0:
        return None, {}, RemainingSeasonOrigin.DERIVED_FULL_SEASON
    active_games = _active_games(nfl_schedule, row.nfl_team_id, weeks)
    games = stats.get("gp")
    raw_fraction = (
        active_games / games
        if games is not None and games > 0
        else 1.0
    )
    return (
        points_per_game * active_games,
        _remaining_stats(stats, raw_fraction, active_games),
        RemainingSeasonOrigin.DERIVED_FULL_SEASON,
    )


def _active_games(nfl_schedule, nfl_team_id, weeks) -> int:
    return sum(
        nfl_schedule.team_week(nfl_team_id, week).status
        is NflTeamWeekStatus.SCHEDULED
        for week in weeks
    )


def _remaining_stats(stats, fraction, active_games):
    rates = {"avg", "fppg", "rate"}
    result = {
        name: value if name in rates else value * fraction
        for name, value in stats.items()
    }
    if "gp" in result:
        result["gp"] = float(active_games)
    return result


def _weekly(
    common: dict[str, object],
    artifact: GenericTableArtifact,
    row: ProjectionArtifactRow,
    unmatched: bool,
    status: ProjectionStatus,
) -> WeeklyProjection:
    has_game = (
        status is ProjectionStatus.OBSERVED
        and row.opponent_team_id is not None
        and row.is_home is not None
    )
    return WeeklyProjection(
        **common,
        week=artifact.week,
        nfl_team_id=None if unmatched else row.nfl_team_id,
        opponent_team_id=row.opponent_team_id if has_game else None,
        is_home=row.is_home if has_game else None,
        nfl_game_id=(
            canonical_nfl_game_id(
                artifact.season,
                artifact.week,
                row.nfl_team_id,
                row.opponent_team_id,
            )
            if has_game
            else None
        ),
    )
def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("captured_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("captured_at must include a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = (
    "ProjectionArtifactRow",
    "projection_artifact_rows",
    "projection_evidence_from_artifact",
    "projection_provider_records",
)
