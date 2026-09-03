from trade_snapshot.draft_config import (
    DraftLeagueConfig,
    DraftStrategy,
    default_slot_eligibility,
)
from trade_snapshot.draft_history import (
    ActualPlayerWeek,
    ActualWeekStatus,
    DataProvenance,
    HistoricalCorpus,
    HistoricalSeason,
    PreseasonPlayer,
)


def small_draft_config(*, strategies=None):
    slots = ("QB", "RB", "WR", "TE")
    return DraftLeagueConfig(
        name="Four-team fixture",
        team_count=4,
        starting_slots=slots,
        bench_slots=1,
        slot_eligibility=default_slot_eligibility(slots),
        position_limits={"QB": 2, "RB": 2, "WR": 2, "TE": 2},
        scoring_weights={"points": 1.0},
        regular_season_weeks=(1, 2),
        playoff_team_count=4,
        playoff_weeks=(3, 4),
        strategy_counts=strategies or {DraftStrategy.NONE: 4},
    )


def draft_player(position, rank, *, season=2025):
    player_id = f"{season}-{position.lower()}-{rank:02d}"
    projected = 130 - rank * 5 + {"QB": 6, "RB": 4, "WR": 2, "TE": 0}[position]
    weeks = tuple(
        ActualPlayerWeek(
            week,
            ActualWeekStatus.PLAYED,
            {"points": projected / 10 + week + (rank % 2)},
        )
        for week in (1, 2, 3, 4)
    )
    return PreseasonPlayer(
        player_id=player_id,
        display_name=f"{position} Player {rank}",
        position=position,
        eligible_positions=(position,),
        nfl_team_id=f"NFL{rank % 8}",
        bye_week=5,
        nfl_experience_years=rank % 5,
        rookie=rank % 5 == 0,
        first_year_on_team=rank % 3 == 0,
        preseason_features={
            "projected_points": projected,
            "projected_stat.touchdowns": max(0, 12 - rank),
            "projected_stat.optional_metric": None if rank % 2 else rank / 10,
        },
        actual_weeks=weeks,
    )


def small_historical_season(season=2025):
    return HistoricalSeason(
        season=season,
        preseason_as_of=f"{season}-08-20T12:00:00+00:00",
        season_kickoff_at=f"{season}-09-04T00:00:00+00:00",
        available_weeks=(1, 2, 3, 4),
        players=tuple(
            draft_player(position, rank, season=season)
            for position in ("QB", "RB", "WR", "TE")
            for rank in range(1, 7)
        ),
    )


def small_historical_corpus():
    return HistoricalCorpus(
        (small_historical_season(),),
        (
            DataProvenance(
                "deterministic fixture",
                "2026-01-01T00:00:00+00:00",
                "unit tests only",
                "CC0",
                preseason_feature_names=(
                    "projected_points",
                    "projected_stat.optional_metric",
                    "projected_stat.touchdowns",
                ),
                preseason_source_as_of={
                    2025: "2025-08-20T12:00:00+00:00",
                },
            ),
        ),
    )
