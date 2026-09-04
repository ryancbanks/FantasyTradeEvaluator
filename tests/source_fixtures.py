from datetime import datetime, timezone

from trade_snapshot._scenario_random import content_id
from trade_snapshot.capture_schema import RankingHorizon
from trade_snapshot.fantasypros_benchmark import (
    FantasyProsLeagueBenchmark,
    FantasyProsTeamBenchmark,
)
from trade_snapshot.projection_source import (
    HostScoringCompatibility,
    ProjectionAttemptReason,
    ProjectionAttemptStatus,
    ProjectionInputBinding,
    ProjectionPointBasis,
    ProjectionSource,
    ProjectionSourceAttempt,
    ProjectionSourceManifest,
    projection_input_id,
)
from trade_snapshot.projections import RemainingSeasonProjection, WeeklyProjection
from trade_snapshot.source_manifest import LeagueBindingScope, WeeklySourceManifest


def weekly_source_manifest(
    snapshot_id: str = "snapshot-1",
    captured_at: datetime = datetime(2026, 9, 1, tzinfo=timezone.utc),
) -> WeeklySourceManifest:
    return WeeklySourceManifest(
        "league_" + "1" * 32,
        LeagueBindingScope.WORKSPACE,
        "espn",
        snapshot_id,
        captured_at,
        "capleague_" + "2" * 64,
        captured_at,
        True,
    )


def fantasypros_league_benchmark(
    snapshot_id: str = "snapshot-1",
    captured_at: datetime = datetime(2026, 9, 1, tzinfo=timezone.utc),
    team_ids: tuple[str, ...] = ("A", "B"),
) -> FantasyProsLeagueBenchmark:
    return FantasyProsLeagueBenchmark(
        snapshot_id,
        "capleague_" + "2" * 64,
        captured_at,
        tuple(
            FantasyProsTeamBenchmark(
                team_id=team_id,
                team_name=f"Team {team_id}",
                current_rank=index,
                projected_rank=index,
                current_wins=0,
                current_losses=0,
                projected_wins=8,
                projected_losses=6,
                playoff_probability=0.5,
                championship_probability=0.1,
            )
            for index, team_id in enumerate(team_ids, 1)
        ),
    )


def projection_source_manifest(
    projection_evidence: tuple[WeeklyProjection | RemainingSeasonProjection, ...],
    *,
    source_scoring_format: str = "PPR",
) -> ProjectionSourceManifest:
    """Create strict synthetic raw lineage for normalized projection test fixtures."""

    if not projection_evidence:
        raise ValueError("projection_evidence must not be empty")
    scoring_ids = {row.scoring_profile_id for row in projection_evidence}
    if len(scoring_ids) != 1:
        raise ValueError("projection_evidence must use one scoring profile")
    sources = []
    attempts = []
    for row in projection_evidence:
        input_id = projection_input_id(row)
        seed = {
            "projection_input_id": input_id,
            "source_scoring_format": source_scoring_format,
        }
        task_id = content_id("captask", seed)
        artifact_id = content_id("captable", seed)
        horizon = (
            RankingHorizon.WEEKLY
            if isinstance(row, WeeklyProjection)
            else RankingHorizon.ROS
        )
        week = (
            row.week
            if isinstance(row, WeeklyProjection)
            else min(row.applicable_weeks, default=1)
        )
        source = ProjectionSource(
            task_id=task_id,
            artifact_id=artifact_id,
            provider=row.provider,
            captured_at=row.captured_at,
            season=row.season,
            week=week,
            horizon=horizon,
            source_scoring_format=source_scoring_format,
            position_scope=("ALL",),
            source_period_text=(
                f"Week {week}" if horizon is RankingHorizon.WEEKLY else "Rest of season"
            ),
            point_basis=ProjectionPointBasis.PROVIDER_TOTAL,
            host_scoring_compatibility=HostScoringCompatibility.BASE_FORMAT_ONLY,
            inputs=(
                ProjectionInputBinding(
                    row.canonical_player_id,
                    row.provider_player_id,
                    input_id,
                ),
            ),
        )
        sources.append(source)
        attempts.append(
            ProjectionSourceAttempt(
                task_id=task_id,
                provider=row.provider,
                season=row.season,
                week=week,
                horizon=horizon,
                scoring=source_scoring_format,
                position_scope=("ALL",),
                attempted_at=row.captured_at,
                status=ProjectionAttemptStatus.CAPTURED,
                reason_code=ProjectionAttemptReason.CAPTURED,
                artifact_id=artifact_id,
            )
        )
    return ProjectionSourceManifest(scoring_ids.pop(), tuple(sources), tuple(attempts))


__all__ = (
    "fantasypros_league_benchmark",
    "projection_source_manifest",
    "weekly_source_manifest",
)
