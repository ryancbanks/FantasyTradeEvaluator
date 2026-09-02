"""Reusable strict browser-capture fixtures."""

from trade_snapshot.capture_schema import LeagueSource, LeagueSourceKind


def league_sources(team_count: int = 2) -> tuple[LeagueSource, ...]:
    team_ids = [str(index) for index in range(1, team_count + 1)]
    player_ids = [str(1000 + index) for index in range(1, team_count + 1)]
    league = {
        "id": "77", "name": "Test League", "team_id": "1", "team_name": "Team 1",
        "season": 2026, "team_count": team_count, "playoff_teams": min(2, team_count),
        "roster_size": 14, "scoring": "PPR",
    }
    teams = [
        {"team_id": team_id, "team_name": f"Team {team_id}"} for team_id in team_ids
    ]
    rosters = [
        {"team_id": team_id, "player_ids": [player_id]}
        for team_id, player_id in zip(team_ids, player_ids)
    ]
    standings = [
        {"teamId": team_id, "wins": 0, "losses": 0, "ties": 0}
        for team_id in team_ids
    ]
    projected = [{
        "teamId": team_id, "teamName": f"Team {team_id}",
        "rank_proj": index, "rank_current": index, "wins_current": 0,
        "losses_current": 0, "wins_proj": 8.0, "losses_proj": 6.0,
        "playoffs_odds": 50.0, "championship_odds": 10.0,
    } for index, team_id in enumerate(team_ids, 1)]
    payloads = {
        LeagueSourceKind.BOOTSTRAP: {
            "current_week": 1, "league": league,
            "players": [
                {"player_id": player_id, "name": f"Player {player_id}",
                 "position_id": "RB", "eligibility": ["RB"]}
                for player_id in player_ids
            ],
            "teams": teams, "rosters": rosters,
        },
        LeagueSourceKind.ANALYZER_INIT: {
            "best_free_agent_ids": ["9001"],
            "standings": standings,
        },
        LeagueSourceKind.PROJECTED_STANDINGS: {
            "playoffsTeam": min(2, team_count), "standings": projected,
        },
    }
    return tuple(
        LeagueSource(source, {"payload": payloads[source]}) for source in LeagueSourceKind
    )


def league_capture_value(team_count: int = 2) -> dict[str, object]:
    return {
        "team_count": team_count,
        "sources": [source.to_record() for source in league_sources(team_count)],
    }
