"""Strict JSON records for a validated fantasy-league state."""

from collections.abc import Mapping

from .league_state import (
    CompletedFantasyMatchup,
    FantasyMatchup,
    HeadToHeadPolicy,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)


_TOP_KEYS = {
    "kind",
    "schema_version",
    "snapshot_id",
    "season",
    "scoring_profile_id",
    "first_remaining_week",
    "teams",
    "standings",
    "remaining_matchups",
    "completed_matchups",
    "roster_rules",
    "playoff_rules",
}

_SCHEMA_VERSION = 2


def league_state_to_record(state: LeagueState) -> dict[str, object]:
    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    rules = state.playoff_rules
    return {
        "kind": "league_state",
        "schema_version": _SCHEMA_VERSION,
        "snapshot_id": state.snapshot_id,
        "season": state.season,
        "scoring_profile_id": state.scoring_profile_id,
        "first_remaining_week": state.first_remaining_week,
        "teams": [
            {"team_id": row.team_id, "name": row.name, "division_id": row.division_id}
            for row in state.teams
        ],
        "standings": [
            {
                "team_id": row.team_id,
                "wins": row.wins,
                "losses": row.losses,
                "ties": row.ties,
                "points_for": row.points_for,
                "points_against": row.points_against,
            }
            for row in state.standings
        ],
        "remaining_matchups": [
            {
                "week": row.week,
                "team1_id": row.team1_id,
                "team2_id": row.team2_id,
                "team1_score_adjustment": row.team1_score_adjustment,
            }
            for row in state.remaining_matchups
        ],
        "completed_matchups": [
            {
                "week": row.week,
                "team1_id": row.team1_id,
                "team2_id": row.team2_id,
                "team1_score": row.team1_score,
                "team2_score": row.team2_score,
            }
            for row in state.completed_matchups
        ],
        "roster_rules": {
            "roster_cap": state.roster_rules.roster_cap,
            "starting_lineup_slots": list(state.roster_rules.starting_lineup_slots),
        },
        "playoff_rules": {
            "qualifier_count": rules.qualifier_count,
            "regular_season_end_week": rules.regular_season_end_week,
            "playoff_weeks": list(rules.playoff_weeks),
            "reseed_each_round": rules.reseed_each_round,
            "division_winner_qualifier_count": rules.division_winner_qualifier_count,
            "tiebreaker_order": [row.value for row in rules.tiebreaker_order],
            "head_to_head_policy": (
                None if rules.head_to_head_policy is None else rules.head_to_head_policy.value
            ),
        },
    }


def league_state_from_record(record: Mapping[str, object]) -> LeagueState:
    if not isinstance(record, Mapping) or set(record) != _TOP_KEYS:
        raise ValueError("league state record fields are invalid")
    if (
        record["kind"] != "league_state"
        or type(record["schema_version"]) is not int
        or record["schema_version"] != _SCHEMA_VERSION
    ):
        raise ValueError("league state record kind or schema version is invalid")
    teams = _rows("teams", record["teams"], {"team_id", "name", "division_id"})
    standings = _rows(
        "standings",
        record["standings"],
        {"team_id", "wins", "losses", "ties", "points_for", "points_against"},
    )
    remaining = _rows(
        "remaining_matchups",
        record["remaining_matchups"],
        {"week", "team1_id", "team2_id", "team1_score_adjustment"},
    )
    completed = _rows(
        "completed_matchups",
        record["completed_matchups"],
        {"week", "team1_id", "team2_id", "team1_score", "team2_score"},
    )
    roster = _object(
        "roster_rules",
        record["roster_rules"],
        {"roster_cap", "starting_lineup_slots"},
    )
    playoff = _object(
        "playoff_rules",
        record["playoff_rules"],
        {
            "qualifier_count",
            "regular_season_end_week",
            "playoff_weeks",
            "reseed_each_round",
            "division_winner_qualifier_count",
            "tiebreaker_order",
            "head_to_head_policy",
        },
    )
    slots = _array("starting_lineup_slots", roster["starting_lineup_slots"])
    weeks = _array("playoff_weeks", playoff["playoff_weeks"])
    raw_tiebreakers = _array("tiebreaker_order", playoff["tiebreaker_order"])
    try:
        tiebreakers = tuple(Tiebreaker(value) for value in raw_tiebreakers)
        head_to_head = (
            None
            if playoff["head_to_head_policy"] is None
            else HeadToHeadPolicy(playoff["head_to_head_policy"])
        )
    except (TypeError, ValueError):
        raise ValueError("league playoff enum value is invalid") from None
    return LeagueState(
        snapshot_id=record["snapshot_id"],
        season=record["season"],
        scoring_profile_id=record["scoring_profile_id"],
        first_remaining_week=record["first_remaining_week"],
        teams=tuple(LeagueTeam(**row) for row in teams),
        standings=tuple(TeamStanding(**row) for row in standings),
        remaining_matchups=tuple(FantasyMatchup(**row) for row in remaining),
        completed_matchups=tuple(CompletedFantasyMatchup(**row) for row in completed),
        roster_rules=RosterRules(roster["roster_cap"], tuple(slots)),
        playoff_rules=PlayoffRules(
            qualifier_count=playoff["qualifier_count"],
            regular_season_end_week=playoff["regular_season_end_week"],
            playoff_weeks=tuple(weeks),
            reseed_each_round=playoff["reseed_each_round"],
            division_winner_qualifier_count=playoff[
                "division_winner_qualifier_count"
            ],
            tiebreaker_order=tiebreakers,
            head_to_head_policy=head_to_head,
        ),
    )


def _rows(name, value, keys):
    rows = _array(name, value)
    if any(not isinstance(row, Mapping) or set(row) != keys for row in rows):
        raise ValueError(f"{name} row fields are invalid")
    return rows


def _object(name, value, keys):
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} fields are invalid")
    return value


def _array(name, value):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value
