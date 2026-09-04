"""Cross-row completeness checks for provider-neutral host league evidence."""

from collections import Counter
from math import fsum, isclose

from .league_state import (
    CompletedFantasyMatchup,
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    TeamStanding,
)


def validate_host_league_snapshot(snapshot) -> None:
    """Reject partial, mixed-league, or internally contradictory host evidence."""

    if len(snapshot.teams) != snapshot.expected_team_count:
        raise ValueError("team coverage does not match expected_team_count")
    teams = _unique_by("source team", snapshot.teams, "source_team_id")
    players = _unique_by("source player", snapshot.players, "source_player_id")
    rosters = _unique_by("roster team", snapshot.rosters, "source_team_id")
    standings = _unique_by("standing team", snapshot.standings, "source_team_id")
    team_ids = set(teams)
    if set(rosters) != team_ids:
        raise ValueError("rosters must contain exactly one row for every source team")
    if set(standings) != team_ids:
        raise ValueError("standings must contain exactly one row for every source team")
    _validate_source_references(snapshot)
    _validate_rosters(snapshot, rosters, players)
    state = _as_source_league_state(snapshot, teams, standings)
    _validate_standing_totals(snapshot, state)
    if snapshot.completed_matchups is not None:
        if not state.completed_history_is_complete:
            raise ValueError("provided completed matchups must cover every elapsed team-week")
        if not state.completed_history_matches_standings:
            raise ValueError("provided completed matchups do not reproduce the standings")


def _validate_source_references(snapshot) -> None:
    rows = (
        *(("team", row.source_team_id, row.provider_ids) for row in snapshot.teams),
        *(("player", row.source_player_id, row.provider_ids) for row in snapshot.players),
    )
    for entity, source_id, references in rows:
        matches = [row for row in references if row.provider == snapshot.source_provider]
        if len(matches) != 1 or getattr(matches[0], f"{entity}_id") != source_id:
            raise ValueError(
                f"each source {entity} must carry its exact source-provider ID"
            )
    _globally_unique_provider_ids("team", snapshot.teams)
    _globally_unique_provider_ids("player", snapshot.players)


def _validate_rosters(snapshot, rosters, players) -> None:
    owners: dict[str, str] = {}
    for team_id, roster in rosters.items():
        occupancy = Counter(roster.reserve_slot_by_player.values())
        unknown = set(occupancy).difference(snapshot.roster_rules.reserve_slot_counts)
        if unknown:
            raise ValueError("source roster uses an unconfigured reserve slot")
        if any(
            count > snapshot.roster_rules.reserve_slot_counts[kind]
            for kind, count in occupancy.items()
        ):
            raise ValueError("source roster exceeds a verified reserve-slot capacity")
        active_size = len(roster.source_player_ids) - len(
            roster.reserve_slot_by_player
        )
        if active_size > snapshot.roster_rules.roster_cap:
            raise ValueError("source roster exceeds the verified roster cap")
        for player_id in roster.source_player_ids:
            if player_id not in players:
                raise ValueError("source roster contains a player without metadata")
            if player_id in owners:
                raise ValueError("a source player cannot be rostered by more than one team")
            owners[player_id] = team_id


def _as_source_league_state(snapshot, teams, standings) -> LeagueState:
    return LeagueState(
        snapshot_id=snapshot.snapshot_id,
        season=snapshot.season,
        scoring_profile_id=snapshot.scoring_profile.scoring_profile_id,
        first_remaining_week=snapshot.first_remaining_week,
        teams=tuple(
            LeagueTeam(row.source_team_id, row.name, row.division_id)
            for row in teams.values()
        ),
        standings=tuple(
            TeamStanding(
                row.source_team_id, row.wins, row.losses, row.ties,
                row.points_for, row.points_against,
            )
            for row in standings.values()
        ),
        remaining_matchups=tuple(
            FantasyMatchup(
                row.week,
                row.source_team1_id,
                row.source_team2_id,
                row.team1_score_adjustment,
            )
            for row in snapshot.remaining_matchups
        ),
        completed_matchups=tuple(
            CompletedFantasyMatchup(
                row.week, row.source_team1_id, row.source_team2_id,
                row.team1_score, row.team2_score,
            )
            for row in (snapshot.completed_matchups or ())
        ),
        roster_rules=snapshot.roster_rules,
        playoff_rules=snapshot.playoff_rules,
    )


def _validate_standing_totals(snapshot, state) -> None:
    elapsed_weeks = snapshot.first_remaining_week - 1
    for standing in state.standings:
        games = standing.wins + standing.losses + standing.ties
        if games != elapsed_weeks:
            raise ValueError("every standing must contain one result per elapsed week")
    if not isclose(
        fsum(row.points_for for row in state.standings),
        fsum(row.points_against for row in state.standings),
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("league points-for and points-against totals must agree")


def _unique_by(name, rows, field):
    result = {}
    for row in rows:
        key = getattr(row, field)
        if key in result:
            raise ValueError(f"{name} IDs must be unique")
        result[key] = row
    return result


def _globally_unique_provider_ids(entity, rows) -> None:
    seen = set()
    for row in rows:
        for reference in row.provider_ids:
            key = (reference.provider, getattr(reference, f"{entity}_id"))
            if key in seen:
                raise ValueError(f"a provider {entity} ID identifies multiple rows")
            seen.add(key)


__all__ = ("validate_host_league_snapshot",)
