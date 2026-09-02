"""Complete provider-neutral league fixtures shared by ingestion tests."""

from datetime import datetime, timezone

from trade_snapshot.league_source import (
    ProviderPlayerId,
    ProviderTeamId,
    SourceCompletedMatchup,
    SourceLeaguePlayer,
    SourceLeagueTeam,
    SourceMatchup,
    SourceTeamRoster,
    SourceTeamStanding,
    VerifiedHostLeagueSnapshot,
)
from trade_snapshot.league_state import PlayoffRules, RosterRules, Tiebreaker
from trade_snapshot.scoring import ScoringProfile


def complete_snapshot(
    team_count: int = 18,
    *,
    completed_history: bool = True,
) -> VerifiedHostLeagueSnapshot:
    if team_count % 2:
        raise ValueError("test fixture requires an even team count")
    teams = tuple(
        SourceLeagueTeam(
            str(index),
            f"Team {index}",
            (
                ProviderTeamId("fantasypros", str(index)),
                ProviderTeamId("espn", f"espn-{index}"),
            ),
        )
        for index in range(1, team_count + 1)
    )
    players = tuple(
        SourceLeaguePlayer(
            f"p{index}",
            f"Player {index}",
            "RB",
            "ARI",
            ("RB", "FLEX"),
            (
                ProviderPlayerId("fantasypros", f"p{index}"),
                ProviderPlayerId("espn", f"e{index}"),
            ),
        )
        for index in range(1, team_count + 1)
    )
    rosters = tuple(
        SourceTeamRoster(str(index), (f"p{index}",))
        for index in range(1, team_count + 1)
    )
    completed = tuple(
        SourceCompletedMatchup(1, str(left), str(left + 1), 100 + left, 90 + left)
        for left in range(1, team_count + 1, 2)
    )
    standings = []
    for matchup in completed:
        standings.extend((
            SourceTeamStanding(
                matchup.source_team1_id, 1, 0, 0,
                matchup.team1_score, matchup.team2_score,
            ),
            SourceTeamStanding(
                matchup.source_team2_id, 0, 1, 0,
                matchup.team2_score, matchup.team1_score,
            ),
        ))
    remaining = tuple(
        SourceMatchup(week, str(left), str(left + 1))
        for week in (2, 3)
        for left in range(1, team_count + 1, 2)
    )
    return VerifiedHostLeagueSnapshot(
        snapshot_id="weekly-2026-2",
        captured_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        source_provider="fantasypros",
        source_league_id="league-77",
        season=2026,
        scoring_profile=ScoringProfile(
            "espn",
            {
                "passing": {"yards_per_point": 25, "touchdown": 4},
                "receiving": {"reception": 1, "yards_per_point": 10},
                "rushing": {"yards_per_point": 10, "touchdown": 6},
            },
        ),
        first_remaining_week=2,
        expected_team_count=team_count,
        teams=teams,
        players=players,
        rosters=rosters,
        standings=tuple(standings),
        remaining_matchups=remaining,
        completed_matchups=completed if completed_history else None,
        roster_rules=RosterRules(14, ("RB",)),
        playoff_rules=PlayoffRules(
            qualifier_count=min(8, team_count),
            regular_season_end_week=3,
            playoff_weeks=(4, 5),
            reseed_each_round=True,
            division_winner_qualifier_count=0,
            tiebreaker_order=(
                Tiebreaker.WIN_PERCENTAGE,
                Tiebreaker.POINTS_FOR,
                Tiebreaker.HEAD_TO_HEAD,
            ),
        ),
    )
