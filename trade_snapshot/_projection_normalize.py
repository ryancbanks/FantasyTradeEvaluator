"""Convert parsed provider rows into immutable local projection evidence."""

from datetime import datetime, timezone

from .capture_schema import GenericTableArtifact, RankingHorizon
from .identity import IdentityRegistry
from .identity_match import ProviderPlayerRecord
from .nfl_schedule import canonical_nfl_game_id
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
) -> tuple[WeeklyProjection | RemainingSeasonProjection, ...]:
    if not isinstance(registry, IdentityRegistry):
        raise ValueError("registry must be an IdentityRegistry")
    if artifact.horizon is RankingHorizon.ROS:
        weeks = tuple(applicable_weeks)
        if not weeks:
            raise ValueError("ROS projection normalization requires applicable_weeks")
    else:
        weeks = ()
    captured_at = _time(artifact.captured_at)
    result = []
    for row in projection_artifact_rows(artifact, known_registry=registry):
        identity = registry.lookup(row.identity_provider, row.provider_player_id)
        unmatched = identity is None
        if identity is not None and normalize_position(identity.position) != row.position:
            raise ValueError(
                f"projection position changed for resolved player {row.provider_player_id!r}"
            )
        status = _status(row, artifact.horizon, unmatched)
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
                row.projected_fantasy_points
                if status is ProjectionStatus.OBSERVED
                else None
            ),
            raw_projected_stats=(
                dict(row.raw_projected_stats)
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
                    origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                )
            )
    return tuple(result)


def _status(
    row: ProjectionArtifactRow,
    horizon: RankingHorizon,
    unmatched: bool,
) -> ProjectionStatus:
    if unmatched:
        return ProjectionStatus.UNMATCHED_PLAYER
    if row.is_bye and horizon is RankingHorizon.WEEKLY:
        return ProjectionStatus.BYE
    if row.projected_fantasy_points is not None:
        return ProjectionStatus.OBSERVED
    return ProjectionStatus.NOT_PUBLISHED


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
