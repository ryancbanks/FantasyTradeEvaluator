"""Allowlist the ESPN fields required by the local league model.

ESPN responses may contain account, owner, or member metadata alongside the
league data we need.  Project at every transport boundary so that unrelated
provider fields never enter the application, its logs, or weekly bundles.
"""

from collections.abc import Mapping
import math


def project_espn_league_payload(value: object) -> dict[str, object]:
    """Return only league, roster, scoring, and schedule fields we consume."""

    league = _record(value)
    settings = _record(league.get("settings"))
    roster_settings = _record(settings.get("rosterSettings"))
    schedule_settings = _record(settings.get("scheduleSettings"))
    scoring_settings = _record(settings.get("scoringSettings"))
    result = _pick(league, ("id", "seasonId", "scoringPeriodId"))
    result.update(
        {
            "status": _pick(
                _record(league.get("status")),
                ("currentMatchupPeriod", "finalScoringPeriod"),
            ),
            "settings": {
                "rosterSettings": {
                    "lineupSlotCounts": _numeric_map(
                        roster_settings.get("lineupSlotCounts")
                    )
                },
                "scheduleSettings": {
                    **_pick(
                        schedule_settings,
                        (
                            "matchupPeriodCount",
                            "playoffTeamCount",
                            "playoffReseed",
                            "playoffSeedingRule",
                        ),
                    ),
                    "divisions": [
                        _pick(_record(division), ("id", "name"))
                        for division in _rows(schedule_settings.get("divisions"))
                    ],
                },
                "scoringSettings": {
                    **_pick(
                        scoring_settings,
                        (
                            "allowOutOfPositionScoring",
                            "homeTeamBonus",
                            "matchupTieRule",
                            "matchupTieRuleBy",
                            "playerRankType",
                            "playoffHomeTeamBonus",
                            "playoffMatchupTieRule",
                            "playoffMatchupTieRuleBy",
                            "scoringType",
                        ),
                    ),
                    "scoringItems": [
                        _project_scoring_item(item)
                        for item in _rows(scoring_settings.get("scoringItems"))
                    ],
                },
            },
            "teams": [_project_team(team) for team in _rows(league.get("teams"))],
            "schedule": [
                _project_matchup(matchup)
                for matchup in _rows(league.get("schedule"))
            ],
            "transactions": [
                _project_transaction(transaction)
                for transaction in _rows(league.get("transactions"))
            ],
        }
    )
    return result


def project_espn_pro_team_payload(value: object) -> dict[str, object]:
    """Return only public NFL-team and schedule fields used by adapters."""

    source = _record(value)
    settings = _record(source.get("settings"))
    return {
        **_pick(source, ("display",)),
        "settings": {
            **_pick(
                settings,
                (
                    "defaultDraftPosition",
                    "draftLobbyMinimumLeagueCount",
                    "gameNotificationSettings",
                    "gated",
                    "playerOwnershipSettings",
                    "readOnly",
                    "statIdToOverridePosition",
                    "teamActivityEnabled",
                    "typeNames",
                ),
            ),
            "proTeams": [_project_pro_team(team) for team in _rows(settings.get("proTeams"))],
        }
    }


def _project_player(value: object) -> dict[str, object]:
    player = _pick(
        _record(value),
        (
            "id",
            "fullName",
            "defaultPositionId",
            "proTeamId",
            "eligibleSlots",
            "injuryStatus",
        ),
    )
    eligible = player.get("eligibleSlots")
    if isinstance(eligible, list):
        player["eligibleSlots"] = list(eligible)
    return player


def _project_roster_entry(value: object) -> dict[str, object]:
    source = _record(value)
    entry = _pick(source, ("playerId", "lineupSlotId"))
    pool_entry = _record(source.get("playerPoolEntry"))
    entry["playerPoolEntry"] = {"player": _project_player(pool_entry.get("player"))}
    return entry


def _project_team(value: object) -> dict[str, object]:
    team = _record(value)
    result = _pick(team, ("id", "name", "abbrev", "divisionId"))
    result["record"] = {
        "overall": _pick(
            _record(_record(team.get("record")).get("overall")),
            ("wins", "losses", "ties", "pointsFor", "pointsAgainst"),
        )
    }
    result["roster"] = {
        "entries": [
            _project_roster_entry(entry)
            for entry in _rows(_record(team.get("roster")).get("entries"))
        ]
    }
    return result


def _project_matchup(value: object) -> dict[str, object]:
    source = _record(value)
    result = _pick(source, ("matchupPeriodId",))
    result["home"] = _pick(_record(source.get("home")), ("teamId", "totalPoints"))
    result["away"] = _pick(_record(source.get("away")), ("teamId", "totalPoints"))
    return result


def _project_scoring_item(value: object) -> dict[str, object]:
    source = _record(value)
    result = _pick(source, ("statId", "points", "isReverseItem"))
    if "pointsOverrides" in source:
        result["pointsOverrides"] = _numeric_map(source.get("pointsOverrides"))
    return result


def _project_transaction(value: object) -> dict[str, object]:
    source = _record(value)
    result = _pick(
        source,
        (
            "acceptedDate",
            "bidAmount",
            "executionType",
            "expirationDate",
            "id",
            "isActingAsTeamOwner",
            "isLeagueManager",
            "isPending",
            "processDate",
            "proposedDate",
            "rating",
            "relatedTransactionId",
            "scoringPeriodId",
            "skipTransactionCounters",
            "status",
            "subOrder",
            "teamId",
            "type",
        ),
    )
    result["items"] = [
        _pick(
            _record(item),
            (
                "fromLineupSlotId",
                "fromTeamId",
                "isKeeper",
                "overallPickNumber",
                "playerId",
                "toLineupSlotId",
                "toTeamId",
                "type",
            ),
        )
        for item in _rows(source.get("items"))
    ]
    if "teamActions" in source:
        result["teamActions"] = {
            key: item
            for key, item in _record(source.get("teamActions")).items()
            if isinstance(key, str) and isinstance(item, str)
        }
    return result


def _project_pro_team(value: object) -> dict[str, object]:
    source = _record(value)
    result = _pick(
        source,
        ("abbrev", "byeWeek", "id", "location", "name", "universeId"),
    )
    raw_schedule = _record(source.get("proGamesByScoringPeriod"))
    result["proGamesByScoringPeriod"] = {
        key: [_project_pro_game(game) for game in _rows(games)]
        for key, games in raw_schedule.items()
        if isinstance(key, str) and _is_integer_text(key)
    }
    if "teamPlayersByPosition" in source:
        # The schedule validator only needs proof that this is an object.  Player
        # membership is unrelated to league analysis and is deliberately dropped.
        result["teamPlayersByPosition"] = {}
    return result


def _project_pro_game(value: object) -> dict[str, object]:
    return _pick(
        _record(value),
        (
            "awayProTeamId",
            "date",
            "homeProTeamId",
            "id",
            "scoringPeriodId",
            "startTimeTBD",
            "statsOfficial",
            "validForLocking",
        ),
    )


def _record(value: object) -> Mapping[object, object]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _pick(value: Mapping[object, object], keys: tuple[str, ...]) -> dict[str, object]:
    return {key: value[key] for key in keys if key in value}


def _numeric_map(value: object) -> dict[str, int | float]:
    result = {}
    for key, item in _record(value).items():
        if (
            isinstance(key, str)
            and _is_integer_text(key)
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(item)
        ):
            result[key] = item
    return result


def _is_integer_text(value: str) -> bool:
    return bool(value) and (value.isdigit() or (value.startswith("-") and value[1:].isdigit()))


__all__ = ("project_espn_league_payload", "project_espn_pro_team_payload")
