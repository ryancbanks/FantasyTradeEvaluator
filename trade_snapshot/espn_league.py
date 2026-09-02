"""Strict ESPN host-league adapter for one weekly, local-only snapshot.

The browser collector may read ESPN's own league and pro-team response bodies
once.  This adapter immediately projects only calculation-relevant public
fantasy data into the provider-neutral boundary; owners, members, tokens, and
transport metadata are never retained.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from numbers import Real

from ._scenario_random import content_id
from .league_source import (
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
from .league_state import HeadToHeadPolicy, PlayoffRules, RosterRules, Tiebreaker
from .scoring import ScoringProfile


ESPN_LEAGUE_ADAPTER_VERSION = "espn-ffl-v3-h2h-points-v3-typed-rosters"

_DEFAULT_POSITION = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    9: "DL",
    10: "DL",
    11: "LB",
    14: "DB",
    16: "DST",
}
_STARTING_SLOT = {
    0: "QB",
    2: "RB",
    3: "RB_WR",
    4: "WR",
    5: "WR_TE",
    6: "TE",
    7: "OP",
    8: "DL",
    9: "DL",
    10: "LB",
    11: "DL",
    12: "DB",
    13: "DB",
    14: "DB",
    15: "IDP",
    16: "DST",
    17: "K",
    23: "FLEX",
}
_NONSTARTING_SLOT = {20: "BENCH", 21: "IR", 25: "ROOKIE_RESERVE"}
_RESERVE_SLOT = {21: "IR", 25: "ROOKIE_RESERVE"}
_SUPPORTED_SLOT_IDS = frozenset(_STARTING_SLOT) | frozenset(_NONSTARTING_SLOT)


def espn_lineup_slot_name(slot_id: int) -> str:
    """Return the shared semantic name for one supported ESPN lineup slot."""

    if isinstance(slot_id, bool) or not isinstance(slot_id, int):
        raise ValueError("ESPN lineup slot ID must be an integer")
    try:
        return (_STARTING_SLOT | _NONSTARTING_SLOT)[slot_id]
    except KeyError:
        raise ValueError(f"ESPN lineup slot {slot_id} is unsupported") from None


def espn_host_league_snapshot(
    league_payload: Mapping[str, object],
    pro_team_payload: Mapping[str, object],
    *,
    captured_at: datetime,
    expected_team_count: int = 18,
) -> VerifiedHostLeagueSnapshot:
    """Normalize a complete ESPN FFL v3 response or fail without guessing."""

    league = _object("league_payload", league_payload)
    pro_teams = _pro_team_map(pro_team_payload)
    captured = _aware(captured_at)
    season = _integer("seasonId", league.get("seasonId"), minimum=2012)
    league_id = _identifier("league id", league.get("id"))
    if type(expected_team_count) is not int or expected_team_count < 2:
        raise ValueError("expected_team_count must be an integer of at least 2")
    teams_raw = _array("teams", league.get("teams"))
    schedule_raw = _array("schedule", league.get("schedule"))
    settings = _object("settings", league.get("settings"))
    status = _object("status", league.get("status"))
    if len(teams_raw) != expected_team_count:
        raise ValueError("ESPN team count does not match expected_team_count")

    schedule_settings = _object(
        "settings.scheduleSettings", settings.get("scheduleSettings")
    )
    roster_settings = _object("settings.rosterSettings", settings.get("rosterSettings"))
    scoring_settings = _object(
        "settings.scoringSettings", settings.get("scoringSettings")
    )
    scoring_type = _text("scoringType", scoring_settings.get("scoringType"))
    if scoring_type not in {"H2H_POINTS", "H2H_POINTS_BASED"}:
        raise ValueError("ESPN adapter currently requires H2H points scoring")
    first_remaining_week = _integer(
        "scoringPeriodId", league.get("scoringPeriodId"), minimum=1
    )
    current_matchup = _integer(
        "status.currentMatchupPeriod", status.get("currentMatchupPeriod"), minimum=1
    )
    if first_remaining_week != current_matchup:
        raise ValueError("ESPN scoring and matchup periods disagree")
    regular_end = _integer(
        "matchupPeriodCount", schedule_settings.get("matchupPeriodCount"), minimum=1
    )
    final_week = _integer(
        "status.finalScoringPeriod", status.get("finalScoringPeriod"), minimum=regular_end + 1
    )

    lineup_counts = _slot_counts(roster_settings.get("lineupSlotCounts"))
    roster_cap = sum(
        count
        for slot_id, count in lineup_counts.items()
        if slot_id not in _RESERVE_SLOT
    )
    reserve_slot_counts = {
        kind: lineup_counts.get(slot_id, 0)
        for slot_id, kind in _RESERVE_SLOT.items()
        if lineup_counts.get(slot_id, 0)
    }
    starting_slots = tuple(
        slot
        for slot_id in sorted(_STARTING_SLOT)
        for slot in (_STARTING_SLOT[slot_id],) * lineup_counts.get(slot_id, 0)
    )
    configured_starting_slots = frozenset(starting_slots)
    roster_rules = RosterRules(roster_cap, starting_slots, reserve_slot_counts)

    divisions = _divisions(schedule_settings.get("divisions"))
    team_rows = []
    standing_rows = []
    roster_rows = []
    player_rows = {}
    for raw in teams_raw:
        team = _object("team", raw)
        team_id = _identifier("team.id", team.get("id"))
        division_id = _division_identifier("team.divisionId", team.get("divisionId"))
        if divisions and division_id not in divisions:
            raise ValueError("ESPN team references an unknown division")
        name = _team_name(team)
        team_rows.append(
            SourceLeagueTeam(
                team_id,
                name,
                (ProviderTeamId("espn", team_id),),
                division_id,
            )
        )
        overall = _object("team.record.overall", _object("team.record", team.get("record")).get("overall"))
        standing_rows.append(
            SourceTeamStanding(
                team_id,
                _integer("wins", overall.get("wins"), minimum=0),
                _integer("losses", overall.get("losses"), minimum=0),
                _integer("ties", overall.get("ties"), minimum=0),
                _number("pointsFor", overall.get("pointsFor")),
                _number("pointsAgainst", overall.get("pointsAgainst")),
            )
        )
        entries = _array("team.roster.entries", _object("team.roster", team.get("roster")).get("entries"))
        player_ids = []
        reserve_slot_by_player = {}
        slot_occupancy = {}
        active_count = 0
        for entry_raw in entries:
            entry = _object("roster entry", entry_raw)
            lineup_slot_id = _integer(
                "roster entry lineupSlotId",
                entry.get("lineupSlotId"),
                minimum=0,
            )
            if lineup_slot_id not in _SUPPORTED_SLOT_IDS:
                raise ValueError(
                    f"ESPN roster entry lineup slot {lineup_slot_id} is unsupported"
                )
            slot_occupancy[lineup_slot_id] = slot_occupancy.get(lineup_slot_id, 0) + 1
            source_player_id = _player_identifier("roster playerId", entry.get("playerId"))
            player = _object(
                "playerPoolEntry.player",
                _object("playerPoolEntry", entry.get("playerPoolEntry")).get("player"),
            )
            if _player_identifier("player.id", player.get("id")) != source_player_id:
                raise ValueError("ESPN roster entry and player IDs disagree")
            normalized = _source_player(
                player, pro_teams, configured_starting_slots
            )
            previous = player_rows.get(source_player_id)
            if previous is not None and previous != normalized:
                raise ValueError("ESPN player metadata conflicts between rosters")
            player_rows[source_player_id] = normalized
            player_ids.append(source_player_id)
            if lineup_slot_id in _RESERVE_SLOT:
                reserve_slot_by_player[source_player_id] = _RESERVE_SLOT[
                    lineup_slot_id
                ]
            else:
                active_count += 1
        if any(
            count > lineup_counts.get(slot_id, 0)
            for slot_id, count in slot_occupancy.items()
        ):
            raise ValueError("ESPN roster occupancy exceeds a captured lineup slot count")
        if active_count > roster_cap:
            raise ValueError("ESPN active roster exceeds the captured roster cap")
        roster_rows.append(
            SourceTeamRoster(
                team_id,
                tuple(player_ids),
                reserve_slot_by_player,
            )
        )

    playoff_rules = _playoff_rules(
        schedule_settings,
        regular_end=regular_end,
        final_week=final_week,
        division_count=len(divisions),
    )
    home_team_bonus = _number(
        "homeTeamBonus", scoring_settings.get("homeTeamBonus")
    )
    remaining, completed = _matchups(
        schedule_raw,
        team_ids={row.source_team_id for row in team_rows},
        first_remaining_week=first_remaining_week,
        regular_end=regular_end,
        home_team_bonus=home_team_bonus,
    )
    scoring_profile = ScoringProfile(
        "espn",
        {
            "adapter_version": ESPN_LEAGUE_ADAPTER_VERSION,
            "scoring_settings": _portable_scoring_settings(scoring_settings),
        },
    )
    snapshot_id = content_id(
        "espn-league-snapshot",
        {
            "adapter_version": ESPN_LEAGUE_ADAPTER_VERSION,
            "captured_at": captured.isoformat(),
            "completed": [
                [row.week, row.source_team1_id, row.source_team2_id,
                 row.team1_score, row.team2_score]
                for row in completed
            ],
            "league_id": league_id,
            "players": [
                [row.source_player_id, row.display_name, row.position,
                 row.nfl_team_id, list(row.eligible_slots)]
                for row in sorted(player_rows.values(), key=lambda value: value.source_player_id)
            ],
            "remaining": [
                [
                    row.week,
                    row.source_team1_id,
                    row.source_team2_id,
                    row.team1_score_adjustment,
                ]
                for row in remaining
            ],
            "rosters": [
                [
                    row.source_team_id,
                    list(row.source_player_ids),
                    dict(row.reserve_slot_by_player),
                ]
                for row in roster_rows
            ],
            "roster_rules": {
                "reserve_slot_counts": dict(roster_rules.reserve_slot_counts),
                "roster_cap": roster_rules.roster_cap,
                "starting_lineup_slots": list(roster_rules.starting_lineup_slots),
            },
            "scoring_profile_id": scoring_profile.scoring_profile_id,
            "season": season,
            "standings": [
                [row.source_team_id, row.wins, row.losses, row.ties,
                 row.points_for, row.points_against]
                for row in standing_rows
            ],
        },
    )
    return VerifiedHostLeagueSnapshot(
        snapshot_id=snapshot_id,
        captured_at=captured,
        source_provider="espn",
        source_league_id=league_id,
        season=season,
        scoring_profile=scoring_profile,
        first_remaining_week=first_remaining_week,
        expected_team_count=expected_team_count,
        teams=tuple(team_rows),
        players=tuple(sorted(player_rows.values(), key=lambda value: value.source_player_id)),
        rosters=tuple(roster_rows),
        standings=tuple(standing_rows),
        remaining_matchups=remaining,
        completed_matchups=completed,
        roster_rules=roster_rules,
        playoff_rules=playoff_rules,
    )


def _source_player(player, pro_teams, configured_starting_slots):
    player_id = _player_identifier("player.id", player.get("id"))
    position_id = _integer("defaultPositionId", player.get("defaultPositionId"), minimum=1)
    try:
        position = _DEFAULT_POSITION[position_id]
    except KeyError:
        raise ValueError(f"ESPN default position {position_id} is unsupported") from None
    pro_team_id = _integer("proTeamId", player.get("proTeamId"), minimum=0)
    if pro_team_id not in pro_teams:
        raise ValueError("ESPN player references an unknown pro team")
    eligible = []
    for raw in _array("eligibleSlots", player.get("eligibleSlots")):
        slot_id = _integer("eligible slot", raw, minimum=0)
        if slot_id in _STARTING_SLOT:
            slot = _STARTING_SLOT[slot_id]
            if slot in configured_starting_slots:
                eligible.append(slot)
            continue
        if slot_id not in _NONSTARTING_SLOT:
            raise ValueError(f"ESPN eligible slot {slot_id} is unsupported")
    eligible.append(position)
    return SourceLeaguePlayer(
        player_id,
        _text("player.fullName", player.get("fullName")),
        position,
        pro_teams[pro_team_id],
        tuple(dict.fromkeys(eligible)),
        (ProviderPlayerId("espn", player_id),),
    )


def _matchups(
    schedule,
    *,
    team_ids,
    first_remaining_week,
    regular_end,
    home_team_bonus,
):
    remaining, completed = [], []
    seen = set()
    for raw in schedule:
        row = _object("schedule matchup", raw)
        week = _integer("matchupPeriodId", row.get("matchupPeriodId"), minimum=1)
        if week > regular_end:
            continue
        home = _object("matchup.home", row.get("home"))
        away = _object("matchup.away", row.get("away"))
        home_id = _identifier("home.teamId", home.get("teamId"))
        away_id = _identifier("away.teamId", away.get("teamId"))
        if home_id not in team_ids or away_id not in team_ids or home_id == away_id:
            raise ValueError("ESPN schedule references invalid teams")
        key = week, frozenset((home_id, away_id))
        if key in seen:
            raise ValueError("ESPN schedule contains a duplicate matchup")
        seen.add(key)
        if week < first_remaining_week:
            completed.append(
                SourceCompletedMatchup(
                    week,
                    home_id,
                    away_id,
                    _number("home.totalPoints", home.get("totalPoints")),
                    _number("away.totalPoints", away.get("totalPoints")),
                )
            )
        else:
            remaining.append(
                SourceMatchup(week, home_id, away_id, home_team_bonus)
            )
    return (
        tuple(sorted(remaining, key=lambda value: (value.week, value.source_team1_id))),
        tuple(sorted(completed, key=lambda value: (value.week, value.source_team1_id))),
    )


def _playoff_rules(settings, *, regular_end, final_week, division_count):
    qualifier_count = _integer("playoffTeamCount", settings.get("playoffTeamCount"), minimum=1)
    reseed = settings.get("playoffReseed")
    if not isinstance(reseed, bool):
        raise ValueError("playoffReseed must be a boolean")
    rule = _text("playoffSeedingRule", settings.get("playoffSeedingRule"))
    if rule != "TOTAL_POINTS_SCORED":
        raise ValueError("ESPN playoff seeding rule is not yet supported exactly")
    return PlayoffRules(
        qualifier_count,
        regular_end,
        tuple(range(regular_end + 1, final_week + 1)),
        reseed,
        division_count if division_count > 1 else 0,
        (
            Tiebreaker.WIN_PERCENTAGE,
            Tiebreaker.POINTS_FOR,
            Tiebreaker.HEAD_TO_HEAD,
            Tiebreaker.DIVISION_RECORD,
            Tiebreaker.POINTS_AGAINST,
            Tiebreaker.RANDOM_DRAW,
        ),
        HeadToHeadPolicy.BALANCED_GROUP_WIN_PERCENTAGE,
    )


def _pro_team_map(payload):
    root = _object("pro_team_payload", payload)
    settings = _object("pro_team_payload.settings", root.get("settings"))
    rows = _array("proTeams", settings.get("proTeams"))
    result = {}
    for raw in rows:
        row = _object("pro team", raw)
        team_id = _integer("pro team id", row.get("id"), minimum=0)
        abbreviation = _text("pro team abbrev", row.get("abbrev")).upper()
        if team_id in result or not abbreviation.isalpha() or not 2 <= len(abbreviation) <= 3:
            raise ValueError("ESPN pro-team table is invalid")
        result[team_id] = {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"}.get(
            abbreviation, abbreviation
        )
    if 0 not in result or len(result) < 33:
        raise ValueError("ESPN pro-team table is incomplete")
    return result


def _portable_scoring_settings(value):
    required = {
        "allowOutOfPositionScoring",
        "homeTeamBonus",
        "matchupTieRule",
        "matchupTieRuleBy",
        "playerRankType",
        "playoffHomeTeamBonus",
        "playoffMatchupTieRule",
        "playoffMatchupTieRuleBy",
        "scoringItems",
        "scoringType",
    }
    if not required <= set(value):
        raise ValueError("ESPN scoring settings are incomplete")
    return {key: value[key] for key in sorted(required)}


def _slot_counts(value):
    rows = _object("lineupSlotCounts", value)
    result = {}
    for raw_id, raw_count in rows.items():
        try:
            slot_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError("ESPN lineup slot ID is invalid") from None
        count = _integer("lineup slot count", raw_count, minimum=0)
        if count and slot_id not in _SUPPORTED_SLOT_IDS:
            raise ValueError(f"ESPN lineup slot {slot_id} is unsupported")
        result[slot_id] = count
    if not result or not any(result.get(slot_id, 0) for slot_id in _STARTING_SLOT):
        raise ValueError("ESPN lineup slots are incomplete")
    return result


def _divisions(value):
    if value is None:
        return set()
    result = set()
    for raw in _array("divisions", value, allow_empty=True):
        row = _object("division", raw)
        division_id = _division_identifier("division.id", row.get("id"))
        _text("division.name", row.get("name"))
        if division_id in result:
            raise ValueError("ESPN divisions contain a duplicate ID")
        result.add(division_id)
    return result


def _team_name(team):
    name = team.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    abbreviation = team.get("abbrev")
    return _text("team name", abbreviation)


def _object(name, value):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _array(name, value, *, allow_empty=False):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a {'JSON array' if allow_empty else 'non-empty JSON array'}")
    return value


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _identifier(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a positive decimal ID")
    text = str(value)
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{name} must be a positive decimal ID")
    return text


def _player_identifier(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a nonzero decimal ID")
    text = str(value)
    if not text.lstrip("-").isdigit() or int(text) == 0:
        raise ValueError(f"{name} must be a nonzero decimal ID")
    return text


def _division_identifier(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a nonnegative decimal ID")
    text = str(value)
    if not text.isdigit():
        raise ValueError(f"{name} must be a nonnegative decimal ID")
    return text


def _integer(name, value, *, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _number(name, value):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("captured_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


__all__ = ("ESPN_LEAGUE_ADAPTER_VERSION", "espn_host_league_snapshot")
